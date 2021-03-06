import subprocess
import select
import signal
import socket
import ssl
import sys
import tempfile
import time
import queue
import threading
import socket
import os
from pathlib import Path
from colorama import Fore, init
from http.server import HTTPServer, SimpleHTTPRequestHandler

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

class ProxyHandler:
    shutdown_flag = threading.Event()
    # SSL/TLS (for connection w/ remote proxies)
    ssl_context = None
    # Paths to cert files
    ssl_cert = None
    ssl_key = None
    socks_started = False
    socks_stopped = False
    def __init__(self, proxy_addr, proxy_port, listen_addr, listen_port):
        # Server that handles clients
        self.client_address = proxy_addr
        self.client_port = int(proxy_port)
        self.client_listener_sock = None
        # Server that handles remote proxies
        self.reverse_address = listen_addr
        self.reverse_port = int(listen_port)
        self.reverse_listener_sock = None
        # Active connections from reverse proxies (sockets)
        self.reverse_sockets = queue.Queue()

    # SSL/TLS for connection with remote proxies
    def set_ssl_context(self, certificate=None, private_key=None, verify=True):
        # Create SSL context using highest TLS version available for client & server.
        # Uses system certs (?). verify_mode defaults to CERT_REQUIRED
        ssl_context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH,)
        # Don't check hostname
        ssl_context.check_hostname = False
        # Store paths to cert and key files
        if certificate:
            self.ssl_cert = os.path.abspath(certificate)
            if private_key:
                self.ssl_key = os.path.abspath(private_key)
        else:
            self.ssl_cert, self.ssl_key = create_ssl_cert()
        ssl_context.load_cert_chain(self.ssl_cert, keyfile=self.ssl_key)
        # Don't be a dick about certs:
        if not verify:
            ssl_context.verify_mode = ssl.CERT_OPTIONAL
        self.ssl_context = ssl_context

    # Master thread
    def serve(self):
        connection_poller_t = threading.Thread(target=self.poll_reverse_connections,name="connection_poller")
        connection_poller_t.start()
        if not self.ssl_context:
            pass
        try:
            # Listen for connections from reverse proxies
            reverse_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reverse_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            reverse_listener.settimeout(0.5)
            reverse_listener.bind((self.reverse_address, self.reverse_port))
            self.reverse_listener_sock = reverse_listener
            # TODO: set threadnames, for better logging
            reverse_listener_t = threading.Thread(target=self.listen_for_reverse,args=[reverse_listener, ],name="reverse_listener")
            reverse_listener_t.start()
            print(Fore.CYAN + "[*] " + Fore.RESET +f"Listening for reverse proxies on {self.reverse_address}:{self.reverse_port}")
            # Listen for clients
            client_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            client_listener.settimeout(0.5)
            client_listener.bind((self.client_address, self.client_port))
            self.client_listener_sock = client_listener
            client_listener_t = threading.Thread(target=self.listen_for_client,args=[client_listener, ],name="client_listener")
            client_listener_t.start()
            print(Fore.CYAN + "[*] " + Fore.RESET +f"Listening for clients on {self.client_address}:{self.client_port}")
            # TODO: some sort of monitoring process. Temporarily just join() thread to keep proc going
            while not self.shutdown_flag.is_set():
                time.sleep(0.5)
        except Exception as e:
            raise e
        finally:
            self.kill_local_process()

    # Close all sockets and threads, then exit. Does not send kill signal to remote machines
    def kill_local_process(self):
        self.shutdown_flag.set()
        self.reverse_listener_sock.close()
        self.client_listener_sock.close()
        while not self.reverse_sockets.empty():
            s = self.reverse_sockets.get()
            s.close()
        #sys.exit(0)
        self.socks_stopped = True
        #self._is_running = False

    # Listen for incoming connections from reverse proxies
    def listen_for_reverse(self, listen_socket, backlog=20):
        # Start listening
        listen_socket.listen(backlog)
        while not self.shutdown_flag.is_set():
            # Accept connection, not yet encrypted
            try:
                clear_socket, __ = listen_socket.accept()
            except socket.timeout:
                continue
            except OSError as e:
                if e.errno == 9:
                    return
                else:
                    raise
            # Encrypt connection
            if self.ssl_context:
                reverse_socket = self.ssl_context.wrap_socket(clear_socket, server_side=True)
            else:
                reverse_socket = clear_socket
            # Store socket for use with client later
            self.reverse_sockets.put(reverse_socket)

    # Listen for proxy clients
    def listen_for_client(self, srv_sock, backlog=10):
        srv_sock.listen(backlog)
        while not self.shutdown_flag.is_set():
            try:
                client_socket, address = srv_sock.accept()
            except socket.timeout:
                continue
            # When shutdown signalled, socket is destroyed at some point, raises OSerror errno9
            except OSError as e:
                if e.errno == 9:
                    return
                else:
                    raise
            address = f"{address[0]}:{address[1]}"
            #print(f"[*] Client connected from {address}")
            forward_conn_t = threading.Thread(target=self.forward_connection,args=[client_socket, ],name=f"forward_client_{address}",daemon=True,)
            forward_conn_t.start()
    # Proxy connection between client and remote
    def forward_connection(self, client_socket, reverse_socket=None, wait=5, max_fails=10):
        reverse_socket = self.get_available_reverse(wait=wait, max_attempts=max_fails)
        # Get basic info on client/remote
        client_addr = client_socket.getpeername()
        reverse_addr = reverse_socket.getpeername()
        # debug message
        #print(f"[_] Tunneling {client_addr} through {reverse_addr}")
        # Send reverse_socket "WAKE" message to wake for proxying
        self.wake_reverse(reverse_socket)
        #######################
        # FORWARDING
        ############
        reverse_socket.setblocking(False)
        client_socket.setblocking(False)
        while  not self.shutdown_flag.is_set():
            receivable, __, __ = select.select([reverse_socket, client_socket], [], [])
            for sock in receivable:
                if sock is reverse_socket:
                    data = b''
                    while True:
                        try:
                            buf = reverse_socket.recv(2048)
                        except (BlockingIOError, ssl.SSLWantReadError):
                            break
                        except Exception as e:
                            print(f"[!] Error receiving from remote: {e}")
                            break
                        if len(buf) == 0:
                            break
                        else:
                            data += buf
                    if len(data) != 0:
                        client_socket.sendall(data)
                    else:
                        #print("[!] Reverse proxy disconnected while forwarding!")
                        client_socket.close()
                        reverse_socket.close()
                        return
                if sock is client_socket:
                    data = b''
                    while True:
                        try:
                            buf = client_socket.recv(2048)
                        except BlockingIOError:
                            break
                        except Exception as e:
                            print(f"[!] Error receiving from client: {e}")
                            break
                        if len(buf) == 0:
                            break
                        else:
                            data += buf
                    if len(data) != 0:
                        reverse_socket.sendall(data)
                    else:
                        # Connection is closed
                        #print(f"[x] Closing connection to client {client_addr}. Forwarding complete")
                        client_socket.close()
                        reverse_socket.close()
                        return

    # Return socket connected to reverse proxy
    def get_available_reverse(self, wait=1, max_attempts=5):
        reverse_socket = None
        try:
            reverse_socket = self.reverse_sockets.get()
        # Don't know the specific exception when getting from empty queue (TODO)
        except Exception as e:
            print(f"[!] No reverse proxies available: {e}")
            #print(f"Waiting max {wait * max_attempts} seconds for a proxy")
            for __ in range(max_attempts - 1):
                time.sleep(wait)
                try:
                    reverse_socket = self.reverse_sockets.get()
                    break
                except:
                    pass
            if not reverse_socket:
                print("[!] No proxies showed up! Killing process and exiting...")
                self.kill_local_process()
                raise
        return reverse_socket

    # Check on waiting reverse proxies to see if connection still open
    def poll_reverse_connections(self, timeout=0.2, wait_time=1):
        # Track connections (value is a set())
        self.reverse_connections = dict()
        # TODO: what's the in/out pattern for Queue? Don't want to just check on same sock over and over
        # But also, this should still work even if it did. Just not ideal
        while not (self.shutdown_flag.is_set()):
            if self.reverse_sockets.empty():
                time.sleep(wait_time)
                continue
            # Get a connection to check on
            reverse_sock = self.reverse_sockets.get()
            address = reverse_sock.getpeername()[0]
            sock_id = id(reverse_sock)
            connection_count = self.reverse_sockets.qsize()
            # store current timeout setting
            old_timeout = reverse_sock.gettimeout()
            # set timeout to something short
            reverse_sock.settimeout(timeout)
            try:
                data = reverse_sock.recv(2048)
                # Means connection closed
                if len(data) == 0:
                    try:
                        # Close socket
                        reverse_sock.shutdown(socket.SHUT_RDWR)         # Disallow further reads and writes
                        reverse_sock.close()
                    except OSError as e:
                        # Socket not connected error
                        if e.errno == 57:
                            pass
                        else:
                            print(e)
                    # Remove socket (and possibly host) from reverse_connections
                    self.reverse_connections[address].remove(sock_id)
                    #print(f"[-] Connection to proxy {address} lost ({connection_count} remain)")
                    # Remove host if there are no remaining connections
                    if len(self.reverse_connections[address]) == 0:
                        del self.reverse_connections[address]
                        #print(f"[-] Reverse proxy {address} lost")
            # timeout will happen if connection still open
            except socket.timeout:
                # If address is known
                if self.reverse_connections.get(address, False):
                    if sock_id not in self.reverse_connections[address]:
                        self.reverse_connections[address].add(sock_id)
                        #print(f"[+] Connection to proxy {address} added ({(connection_count + 1)} total)")
                # Address is new
                else:
                    self.reverse_connections[address] = set()
                    self.reverse_connections[address].add(sock_id)
                    print(Fore.GREEN + "[+] " + Fore.RESET + f"Reverse proxy successfully established with: {address}")
                    #global socks_started
                    self.socks_started = True
                    #print(f"[+] Connection to proxy {address} added (1 total)")
                # Set timeout to original value, put back in queue
                reverse_sock.settimeout(old_timeout)
                self.reverse_sockets.put(reverse_sock)         
        return

    # Send 'WAKE' message to waiting reverse proxy. Return reply message
    def wake_reverse(self, reverse_sock, max_attempts=5):
        reply = None 
        reverse_sock.send("WAKE".encode())
        data = reverse_sock.recv(2048)
        i = 0
        while not (len(data) == 4):
            data += reverse_sock.recv(2048)
            if i == max_attempts:
                break
            else:
                i += 1
        if data != b"WOKE":
            print(f"[!] Unexpected reply from reverse proxy: {data}")
            # raise
        else:
            reply = 'WOKE'
        return reply
