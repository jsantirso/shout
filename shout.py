# coding=utf-8

#######################################################################################################################
#
#    First part, definition of the more-or-less generic server.
#
#######################################################################################################################

from urlparse import urlparse
from base64 import b64encode
from hashlib import sha1
from select import select
import os
import threading
import time
import socket
import struct


class CloseConnectionException(Exception):
    """
    An exception used to close the current connection
    """
    pass


class NanoWebServer():
    """
    An implementation of the HTTP and WebSocket standards that covers just enough of them
    for the Shout! application to reasonably work. It's thoroughly untested.
    It has a main thread where it listens for new connections in a blocking manner, and a
    separate thread where it handles further communications with the client sockets.
    For production, Shout! should be deployed on a full-featured server.
    """

    @staticmethod
    def parse_http_request(raw_data):
        """
        A custom, super simple HTTP Request parser for GET requests.
        Receives raw data from the socket and returns a tuple with the following:
            path, string
            params, string
            query, string
            fragment, string
            http_version, string
            headers, dict, all the keys are uppercased (for easier identification, plus they're case-insensitive)
        If the request can't be parsed or the method is not supported, an Exception is raised.
        """
        assert raw_data.find('\r\n\r\n') >= 0

        # We separate the request into header and body
        header, body = raw_data.split('\r\n\r\n', 1)
        # We separate the header into the Request-line and the rest
        request_line, raw_headers = header.split('\r\n', 1)
        # We process the Request-Line
        method, uri, http_version = request_line.split(' ')
        method, http_version = method.upper(), http_version.upper()
        if not method in ('GET', ):
            raise Exception('Unsupported method: %s' % method)
        if not http_version:
            raise Exception('Invalid http version: %s' % http_version)
        scheme, netloc, path, params, query, fragment = urlparse(uri)
        if not path:
            raise Exception('Invalid uri: %s' % uri)
        # We process the headers (remember that the same header might come in multiple lines if it has multiple values:
        # we handle that case creating a single entry with the values separated by commas as per the RFC 2616)
        headers = {}
        for raw_header in raw_headers.split('\r\n'):
            header, value = raw_header.split(': ', 1)
            header_upper = header.upper()
            if header_upper in headers:
                headers[header_upper] += ',' + value
            else:
                headers[header_upper] = value
        return path, params, query, fragment, http_version, headers


    @staticmethod
    def parse_websocket_msg(raw_bytes):
        """
        Receives a byte string and returns a tuple with the following:
            msg_type, string, one of the following:
                "text", the client has sent a text message
                "ping", the client has sent a keepalive message, and is expecting a "pong" in return
                "pong", the client has sent a keepalive message, possibly unidirectional because we don't send pings
            message, string, the client data once the websocket frames have been removed
            processed_bytes, int, the number of bytes from the input that have been used (should possibly be discarded)
        If no message can be built from the input, the tuple (None, '', 0) is returned.
        It might raise a CloseConnectionException if there is a non-recoverable error while parsing the message, or if
        a "Connection close" control frame is received
        """
        # Msg is the payload we are reading from raw_bytes.
        # Pos is our position at raw_bytes.
        # We must define them in a list because otherwise the '+=' inside read() would mark pos as local to its scope,
        # with no way (in python 2.x) to access just one scoping level out from there (there's a global keyword, but
        # not a nonlocal keyword as there is in python 3.x).
        # Defining them in a list is a hack that avoids that problem.
        msg_and_pos = ['', 0]
        
        def read(num_bytes):
            """
            Returns num_bytes from raw_bytes and advances pos.
            If there aren't enough bytes in raw_bytes it raises an exception
            """
            read_bytes = raw_bytes[msg_and_pos[1]:msg_and_pos[1]+num_bytes]
            if len(read_bytes) < num_bytes:
                raise Exception('Not enough info')
            msg_and_pos[1] += num_bytes
            return read_bytes
        
        try:
            while msg_and_pos[1] < len(raw_bytes):
                # We process the info as per the RFC 6455.
                # We must remember that a single message can come divided in multiple websocket frames, thus the loop.
                # First of all, we get the fixed two bytes used for basic control
                control = read(2)
                if not control:
                    raise CloseConnectionException()
                fin = bool(ord(control[0]) & 0b10000000)
                if ord(control[0]) & 0b01110000:
                    # The reserved bits should be 0 because we haven't negotiated an extension
                    raise CloseConnectionException()
                opcode = ord(control[0]) & 0b00001111
                if opcode not in (0, 1, 9, 10):
                    # Which mean: (continuation frame, text frame, ping, pong)
                    # We will fail on any other control frames (including "Connection close")
                    raise CloseConnectionException()
                # We must unmask the incoming data frame to obtain its payload
                masked = bool(ord(control[1]) & 0b10000000)
                if not masked:
                    raise CloseConnectionException()
                payload_len = ord(control[1]) & 0b01111111
                if payload_len == 126:
                    payload_len = struct.unpack('>H', read(2))[0]
                elif payload_len == 127:
                    payload_len = struct.unpack(">Q", read(8))[0]
                mask = read(4)
                
                # We get the payload
                raw_payload = read(payload_len)
                # We unmask the raw payload (remember that the message might continue from a previous frame)
                msg_and_pos[0] += ''.join([chr(ord(char) ^ ord(mask[i % 4])) for i, char in enumerate(raw_payload)])
                
                # We check if the message has been fully received
                if fin:
                    if opcode == 9:
                        msg_type = 'ping'
                    elif opcode == 10:
                        msg_type = 'pong'
                    else:
                        msg_type = 'text'
                    return msg_type, msg_and_pos[0], msg_and_pos[1]
            
            return None, '', 0
            
        except CloseConnectionException:
            raise
        
        except Exception, e:
            # We might not have all the info required to build a message
            return None, '', 0
    
    
    @staticmethod
    def wrap_in_websocket_frame(msg, msg_type='text'):
        """
        Receives a byte string and returns a new string with the original message encapsulated in a websocket frame.
        It admits the following message types: 'text', 'close', 'ping', 'pong'
        """
        opcode = {
            'text':  1,
            'close': 8,
            'ping':  9,
            'pong': 10
        }.get(msg_type, 1)

        framed_msg = chr(0b10000000 | opcode)
        length = len(msg)
        if length <= 125:
            framed_msg += chr(length)
        elif 126 <= length <= 65535:
            framed_msg += chr(126)
            framed_msg += struct.pack(">H", length)
        else:
            framed_msg += chr(127)
            framed_msg += struct.pack(">Q", length)
        framed_msg += msg
        
        return framed_msg
    
    
    def __init__(self, host, port, http_handlers, ws_handlers):
        """
        Initializes the server.
        Receives:
            host, string, might be empty
            port, int
            http_handlers, a list of (path, handler) tuples where:
                path is a string, for example '/'
                handler is a function that will be called for GET requests to that path.
                    It must return a string, which will be served to the client
                    This function receives:
                        path, params, query, fragment and http_version, which are possibly empty strings
                        headers, a dictionary of HTTP Headers (the keys are in uppercase)
                        addr, the address returned by the socket connection
                        state, a dictionary that holds information indefinitely, useful to store server state info
            ws_handlers, a list of (path, on_connection, handler) tuples where:
                path is a string, for example '/'
                on_connection is a function that will be called when the websocket connection is established
                    If this function raises a CloseConnectionException, the connection is shut down.
                    This function receives:
                        path and query, which are possibly empty strings that describe the request
                        message, unicode string, as sent by the client
                        addr, the address returned by the socket connection
                        socket_state, a dict, unique for this websocket, in case the handler wants to save state info
                        state, a dictionary that holds information indefinitely, useful to store server state info
                        send_message, a function that receives an unicode string and schedules it to be sent to the
                            client
                        broadcast, a function that receives an unicode string and schedules it to be sent to every
                            connected websocket
                handler is a function that will be called for each data frame received in that path via WebSockets.
                    This function is executed in a thread.
                    If this function raises a CloseConnectionException, the connection is shut down.
                    This function receives:
                        path and query, which are possibly empty strings that describe the request
                        message, unicode string, as sent by the client
                        addr, the address returned by the socket connection
                        socket_state, a dict, unique for this websocket, in case the handler wants to save state info
                        state, a dictionary that holds information indefinitely, useful to store server state info
                        reply, a function that receives an unicode string and schedules it to be sent to the client
                        broadcast, a function that receives an unicode string and schedules it to be sent to every
                            connected websocket
        """
        # Some vars for the server socket (main thread)
        
        self.host = host
        self.port = port
        
        # Some vars for the connection handler (secondary thread)
        
        self.http_handlers = http_handlers
        self.ws_handlers = ws_handlers
        
        # A dictionary that will be passed to the client functions so that they can store information between requests
        self.client_state = {}
        
        # A map of the currently active sockets.
        # The keys are the sockets themselves, the values are dicts with the following entries:
        #    'addr':         as returned by accept()
        #    'connection':   'http' or 'websocket'
        #    'step':         a numeric value that indicates the internal step in this socket's lifecycle
        #    'info':         a dictionary with the strings path, params, query, fragment, http_version and dict headers
        #    'ws_handlers':  if the connection is an established websocket, the tuple (on_connection, handler)
        #    'received':     a string with the most recent data received from the socket
        #    'outbound':     a list of byte strings that are awaiting to be sent through the socket.
        #                    Each socket has its own list, but messages broadcasted to every socket should be the
        #                    same object in memory (with the same id()) and therefore memory footpring shouldn't be
        #                    too high (except when partial sends have ocurred, because we create copies of the outbound
        #                    messages removing the already sent part)
        #    'client_state': a dictionary with user-defined information
        self.sockets = {}
        
        # Some vars that will be used in the server (main thread) and client sockets (secondary thread)
        
        # A list of (socket, addr) tuples, scheduled for handling. This list allows us to only use the dict of sockets
        # in one thread therefore avoiding the need of locks to handle it
        self.new_connections = []
        # A lock that will let us manage that var synchronously
        self.lock = threading.RLock()
        
        
    def start(self):
        """
        Starts listening indefinitely for new connections in the main thread, and creates a new
        thread to handle them.
        """
        # We create the secondary thread
        threading.Thread(target=self.handle_connections).start()
        
        # We create the main socket that will accept new connections
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((self.host, self.port))
        s.listen(5)  # Max queued connections
        while True:
            # We wait (block) for a new connection, which will create a new socket (conn)
            conn, addr = s.accept()
            # We make the new socket non-blocking
            conn.setblocking(0)
            # We schedule that socket to be handled in the secondary thread
            self.lock.acquire()
            self.new_connections.insert(0, (conn, addr))
            self.lock.release()
    
    
    def handle_connections(self):
        """
        Handles current socket connections.
        Info about connections and steps:
            http:
                0:  new connection, must read and parse the request and prepare the response accordingly
                1:  request read and parsed, the response is ready to be sent (nothing else must be read)
            websocket:
                0: upgrade to websocket protocol requested by the client, we prepare the response
                1:  the response to the upgrade handshake is ready to be sent (nothing else must be read)
                2:  a websocket connection has been stablished and is open to full-duplex communication
        """
        def close_connection_and_remove(s):
            """
            Closes the connection and removes the socket from the dict
            """
            try:
                s.shutdown(socket.SHUT_RDWR)
                s.close()
            finally:
                del self.sockets[s]
        
        def send_to_websocket(s, msg):
            """
            Receives a socket, a string, wraps it in a websocket frame, and enqueues it in the socket's outbound list.
            If the outbound list is too big this function closes the connection
            """
            framed_msg = NanoWebServer.wrap_in_websocket_frame(msg)
            self.sockets[s]['outbound'].append(framed_msg)
            if len(self.sockets[s]['outbound']) >= 300:
                # We close the connection
                close_connection_and_remove(s)

        def send_to_active_websockets(msg):
            """
            Receives a string, wraps it in a websocket frame, and enqueues it the outbound list of every active
            websocket
            """
            framed_msg = NanoWebServer.wrap_in_websocket_frame(msg)
            for s in self.sockets.keys():
                if self.sockets[s]['connection'] == 'websocket' and self.sockets[s]['step'] == 2:
                    self.sockets[s]['outbound'].append(framed_msg)
                    if len(self.sockets[s]['outbound']) >= 300:
                        # We close the connection
                        close_connection_and_remove(s)

        def send_pong_to_websocket(s, ping_payload):
            """
            Enqueues a "pong" control frame in the given websocket's outbound list
            """
            framed_msg = NanoWebServer.wrap_in_websocket_frame(ping_payload, 'pong')
            self.sockets[s]['outbound'].append(framed_msg)
            
        while True:
            # We process new connections
            self.lock.acquire()
            while self.new_connections:
                s, a = self.new_connections.pop()
                self.sockets[s] = {
                    'addr': a,
                    'connection': 'http',  # Sockets are treated as http by default
                    'step': 0,  # An internal step used while handling the connection
                    'info': {'path': '', 'params': '', 'query': '', 'fragment': '', 'http_version': '', 'headers': {}},
                    'ws_handlers': None,
                    'received': '',
                    'outbound': [],
                    'client_state': {}
                }
                print '\nNew connection from %s (fileno %s)' % (a, s.fileno())  # For debugging purposes
            self.lock.release()
            
            try:
                # We prepare the sockets that need handling
                rlist = [s for s in self.sockets.keys() if
                         (self.sockets[s]['connection'] == 'http' and self.sockets[s]['step'] == 0) or
                         (self.sockets[s]['connection'] == 'websocket' and self.sockets[s]['step'] == 2)]
                wlist = [s for s in self.sockets.keys() if self.sockets[s]['outbound']]
                if not rlist and not wlist:
                    # We have nothing to do right now
                    # print 'Nothing to do, sleeping'  # For debugging purposes
                    time.sleep(0.5)
                    continue
                
                # We handle the current connections
                ready_to_read, ready_to_write, _ = \
                    select(
                        rlist,
                        wlist,
                        [],  # xlist
                        2.0  # timeout
                    )
                # print 'Select has succeeded: rlist (%s), wlist (%s)' % (
                #     ', '.join([str(s.fileno()) for s in rlist]),
                #     ', '.join([str(s.fileno()) for s in wlist])
                # )  # For debugging purposes
            except Exception, e:
                print Exception, e
                # print 'Select has failed'  # For debugging purposes
                # Select has failed, we must find the culprit in a really ugly way
                try:
                    for s in self.sockets.keys():
                        select([s], [], [], 0)
                except:
                    # print 'Culprit found: %s' % s.fileno()  # For debugging purposes
                    # We abort the bad connection (we don't bother sending the proper control frame
                    # if it's a websocket)
                    close_connection_and_remove(s)
    
                # We can't continue this turn
                continue
    
            # If we are here, we might have some work to do
            
            # Reading:
            for s in ready_to_read:
                try:
                    # print 'Ready to read %s' % s.fileno()  # For debugging purposes
                    
                    # Is it a new connection?
                    if self.sockets[s]['connection'] == 'http' and self.sockets[s]['step'] == 0:
                        self.sockets[s]['received'] += s.recv(2048)

                        # print self.sockets[s]['received']  # For debugging purposes

                        if self.sockets[s]['received'].find('\r\n\r\n') >= 0:
                            # We might have a complete request
                            path, params, query, fragment, http_version, headers = \
                                NanoWebServer.parse_http_request(self.sockets[s]['received'])
                            # print path, params, query, fragment, http_version, headers  # For debugging purposes
                            print path  # For debugging purposes
                            
                            # We discard the received info we have just processed
                            self.sockets[s]['received'] = ''
                            
                            # Looks like a valid request, we update the socket's connection info
                            self.sockets[s]['info']['path'] = path
                            self.sockets[s]['info']['params'] = params
                            self.sockets[s]['info']['query'] = query
                            self.sockets[s]['info']['fragment'] = fragment
                            self.sockets[s]['info']['http_version'] = http_version
                            self.sockets[s]['info']['headers'] = headers
                            
                            # We check if the connection is trying to upgrade from HTTP to WebSocket.
                            # Firefox 28 sends multiple values in the connection header ('keep-alive, Upgrade'),
                            # Chrome 33 doesn't ('Upgrade'), neither does IE11 ('Upgrade')
                            if 'upgrade' in [c.strip().lower() for c in headers.get('CONNECTION', '').split(',')] and \
                               'ORIGIN' in headers and headers.get('UPGRADE', '').lower() == 'websocket':
                                # It's a websocket request, which right now is in the handshake step

                                # We handle the connection
                                response = ''
                                for handler_path, handler_on_connection, handler_function in self.ws_handlers:
                                    if handler_path == path:
                                        # We advance the socket's lifecycle
                                        self.sockets[s]['connection'] = 'websocket'
                                        self.sockets[s]['step'] = 0  # (this is not really necessary)
                                        
                                        # We process the request, still in vanilla HTTP
                                        # We create the Websocket Accept key
                                        accept_key = headers['SEC-WEBSOCKET-KEY']
                                        accept_key += '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'  # WebSockets' constant
                                        accept_key = b64encode(sha1(accept_key).hexdigest().decode('hex')).strip()
                                        # We build the HTTP response to the handshake
                                        # Notice that we reject any extensions offered by the client by simply not
                                        # including that header
                                        response = 'HTTP/1.1 101 Switching Protocols\r\n'
                                        response += 'Upgrade: websocket\r\n'
                                        response += 'Connection: Upgrade\r\n'
                                        response += 'Sec-WebSocket-Accept: %s\r\n' % accept_key
                                        response += '\r\n'
                                        
                                        # We advance the socket's lifecycle
                                        self.sockets[s]['step'] = 1
                                        
                                        # We store a copy of the handlers for easier (O(1)) access in the future
                                        self.sockets[s]['ws_handlers'] = (handler_on_connection, handler_function)
                                        break
                                
                                if not response:
                                    # The rest of the uris will serve a 404
                                    response = 'HTTP/1.1 404 Not Found\r\n'
                                    response += 'Content-Type: text/html; charset=UTF-8\r\n'
                                    response += '\r\n'
                                    
                                    # We advance the socket's lifecycle (still in 'http' connection)
                                    self.sockets[s]['step'] = 1
                                
                                # We enqueue the response
                                self.sockets[s]['outbound'].append(response)
                                
                            else:
                                # It's a simple http request
                                
                                # We try to serve the request matching the path against those handled by the server
                                response = ''
                                for handler_path, handler_function in self.http_handlers:
                                    if handler_path == path:
                                        # We process the request
                                        body = handler_function(path, params, query, fragment, http_version, headers,
                                                                self.sockets[s]['addr'], self.client_state)
                                        response = 'HTTP/1.1 200 OK\r\n'
                                        response += 'Content-Type: text/html; charset=UTF-8\r\n'
                                        response += 'Content-Length: %s\r\n' % len(body)
                                        response += '\r\n'
                                        response += body
                                        break
                                        
                                if not response:
                                    # The rest of the uris will serve a 404
                                    response = 'HTTP/1.1 404 Not Found\r\n'
                                    response += 'Content-Type: text/html; charset=UTF-8\r\n'
                                    response += '\r\n'
                                
                                # We enqueue the response
                                self.sockets[s]['outbound'].append(response)
                                
                                # We advance the socket's lifecycle
                                self.sockets[s]['step'] = 1
                            
                        else:
                            # We have received additional info but not a complete GET request
                            # We check the size of the buffer to counter potential attacks
                            if len(self.sockets[s]['received']) >= 65535:
                                # We will abort this connection
                                print 'Attacking HTTP connection aborted (%s bytes in buffer)' % \
                                      len(self.sockets[s]['received'])  # For debugging purposes
                                raise CloseConnectionException()

                            # Otherwise, we wait a cycle
                            # print 'Partial request received from %s' % self.sockets[s]['received']  # For dbg. purp.
                            pass
                    
                    # Is it an open websocket connection?
                    elif self.sockets[s]['connection'] == 'websocket' and self.sockets[s]['step'] == 2:
                        self.sockets[s]['received'] += s.recv(2048)

                        # We check the size of the buffer to counter potential attacks
                        if len(self.sockets[s]['received']) >= 65535:
                            # We will abort this connection
                            print 'Attacking WebSocket connection aborted (%s bytes in buffer)' % \
                                  len(self.sockets[s]['received'])  # For debugging purposes
                            raise CloseConnectionException()

                        while True:
                            # We try to parse a message from the received data
                            # We do it inside a loop to exhaust the buffer because we might have received
                            # multiple messages

                            message_type, message, processed_bytes = \
                                NanoWebServer.parse_websocket_msg(self.sockets[s]['received'])

                            if message_type is None:
                                # We don't have a full message in the received buffer; we keep listening
                                break

                            else:
                                # We must discard the already processed bytes from the received buffer
                                self.sockets[s]['received'] = self.sockets[s]['received'][processed_bytes:]

                                if message_type == 'text':
                                    # We have received a full message from the client
                                    # We call the handler
                                    self.sockets[s]['ws_handlers'][1](
                                        self.sockets[s]['info']['path'],
                                        self.sockets[s]['info']['query'],
                                        message.decode('utf-8'),
                                        self.sockets[s]['addr'],
                                        self.sockets[s]['client_state'],
                                        self.client_state,
                                        lambda msg: send_to_websocket(s, msg.encode('utf-8')),
                                        lambda msg: send_to_active_websockets(msg.encode('utf-8'))
                                    )

                                elif message_type == 'ping':
                                    # The client has sent a "ping" control frame, we must answer with a "pong"
                                    # containing the exact same payload.
                                    # These are special keep-alive / heartbeat messages.
                                    print 'We have received a PING from', self.sockets[s]['addr']  # For dbg. purposes
                                    send_pong_to_websocket(s, message)

                                elif message_type == 'pong':
                                    # A possibly unsolicited "pong" message (because we don't send pings), a response
                                    # is not expected
                                    print 'We have received a PONG from', self.sockets[s]['addr']  # For dbg. purposes
                                    pass

                except CloseConnectionException:
                    # We close the connection
                    close_connection_and_remove(s)
                
                except Exception, e:
                    print Exception, e
                    # We close the connection
                    close_connection_and_remove(s)
            
            # Writing:
            for s in ready_to_write:
                # print 'Ready to write %s' % s.fileno()  # For debugging purposes
                
                sent = s.send(self.sockets[s]['outbound'][0])
                if sent == len(self.sockets[s]['outbound'][0]):
                    # We have sent the full response, we remove it from the queue
                    self.sockets[s]['outbound'] = self.sockets[s]['outbound'][1:]
                    
                    if self.sockets[s]['connection'] == 'http' and self.sockets[s]['step'] == 1:
                        # We close the connection
                        # print 'Closing http connection: %s' % s.fileno()  # For debugging purposes
                        close_connection_and_remove(s)
                    
                    elif self.sockets[s]['connection'] == 'websocket' and self.sockets[s]['step'] == 1:
                        # A websocket connection has been successfully established
                        
                        # print 'Websocket connection established (fileno %s)' % s.fileno()  # For debugging purposes
                        
                        # We advance the socket's lifecycle
                        self.sockets[s]['step'] = 2
                        
                        # We execute the callback
                        self.sockets[s]['ws_handlers'][0](
                            self.sockets[s]['info']['path'],
                            self.sockets[s]['info']['query'],
                            self.sockets[s]['addr'],
                            self.sockets[s]['client_state'],
                            self.client_state,
                            lambda msg: send_to_websocket(s, msg.encode('utf-8')),
                            lambda msg: send_to_active_websockets(msg.encode('utf-8'))
                        )
                    
                else:
                    # We have only sent part of the response, we update the queue
                    self.sockets[s]['outbound'][0] = self.sockets[s]['outbound'][0][sent:]


#######################################################################################################################
#
#    Second part, definition of the logic pertaining to the chat.
#    Here, messages are handled as unicode objects to avoid the following problems related to multi-byte characters:
#        - Counting: len(<str>) returns the number of bytes, not necessarily the number of complete characters
#        - Slicing: <str>[-1] returns the last byte, not necessarily the last complete character
#    If we didn't decode to unicode before handling, we could really mess up the strings (leaving incomplete chars,
#    etc.) and the logic.
#
#######################################################################################################################

# HTTP HANDLER(S)

# We load the frontend from its file for fast future access
with open('shout.html') as f:
    shout_frontend = f.read()


# We define the http handler
def serve_frontend(path, params, query, fragment, http_version, headers, addr, state):
    return shout_frontend

#######################################################################################################################

# WEBSOCKET HANDLER(S) AND CALLBACK(S)


# We create a function that converts a number to an arbitrary base (we'll use it for the user ids)
def base_n(num, base='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'):
    return ((num == 0) and base[0]) or (base_n(num // len(base), base).lstrip(base[0]) + base[num % len(base)])


# We define a callback for when a chat connection is ready
def on_chat_connection(path, query, addr, socket_state, state, send_message, broadcast):
    # Remember that send_message and broadcast receive unicode objects

    # We prepare this user's code (he doesn't have a username yet)
    if not 'next_id' in state:
        state['next_id'] = 1000 # We start with a high number for aesthetics
    socket_state['usercode'] = base_n(state['next_id'])
    state['next_id'] += 1
    
    # print 'Chat connection established (usercode %s)' % socket_state['usercode']  # For debugging purposes
    
    # We send him the list of recent messages
    for message in state.get('msg_backlog', []):
        send_message(message)


# We define the websocket handler
def serve_chat(path, query, message, addr, socket_state, state, reply, broadcast):
    # Remember that message is an unicode object, and that reply and broadcast receive unicode objects
    
    # print 'Handling chat message (usercode %s): %s' % (socket_state['usercode'], message)  # For debugging purposes
    
    if not message:
        return
    
    if message.startswith('usr:'):
        # We use this prefix for the message where the user selects his username.
        # We must sanitize the string and escape the "|" character (because we will be using it as
        # a separator in our responses)
        socket_state['username'] = \
            message.replace('usr:', '', 1).replace('\r', '').replace('\n', '')[:30].replace('|', '\\|')

        # We create the rate control dict for the current IP address
        if not 'rate_control' in state:
            state['rate_control'] = {}
        if not addr[0] in state['rate_control']:
            state['rate_control'][addr[0]] = {
                'minute': 0,
                'messages': 0
            }

        # We create a response with this format: 'usr|usercode|username'
        response_message = u'usr|%s|%s' % (
            socket_state['usercode'],
            socket_state['username']
        )
        reply(response_message)
        
    elif message.startswith('msg:'):
        # We use this prefix for the messages that the user wants to send to the chat
        if not 'username' in socket_state or not 'usercode' in socket_state:
            # The socket_state is not valid
            raise CloseConnectionException()

        # We enforce the rate limiting feature: at most 20 messages per minute per IP
        current_epoch_seconds = int(time.time())
        current_minute = current_epoch_seconds / 60  # 0..n, but it doesn't really matter
        next_minute_start = current_epoch_seconds - (current_epoch_seconds % 60) + 60
        messages_per_minute = 20
        if state['rate_control'][addr[0]]['minute'] == current_minute and \
           state['rate_control'][addr[0]]['messages'] >= messages_per_minute:
            # We won't broadcast this message
            # We send an error response (it might reach the user a bit late depending
            # on the number of outbound messages and the conditions of the network)
            reply(u'error|rate|%s' % next_minute_start)

        else:
            # The message is allowed

            # We create a broadcast message with this format: 'msg|<epoch_seconds>|<usercode>|<username>|<msg>'
            broadcast_message = u'msg|%s|%s|%s|%s' % (
                current_epoch_seconds,
                socket_state['usercode'],
                socket_state['username'],  # It's already sanitized and escaped
                # We sanitize and escape the message
                message.replace('msg:', '', 1).replace('\r', '').replace('\n', '')[:140].replace('|', '\\|')
            )

            # We send the message to every connected peer
            broadcast(broadcast_message)

            # We log that response in the msg backlog (up to 25 unicode messages)
            if not 'msg_backlog' in state:
                state['msg_backlog'] = []
            state['msg_backlog'].append(broadcast_message)
            state['msg_backlog'] = state['msg_backlog'][-25:]

            # We update the rate information
            if state['rate_control'][addr[0]]['minute'] == current_minute:
                state['rate_control'][addr[0]]['messages'] += 1
            else:
                state['rate_control'][addr[0]]['minute'] = current_minute
                state['rate_control'][addr[0]]['messages'] = 1

            # We check if we must send a rate limit warning
            if state['rate_control'][addr[0]]['messages'] >= messages_per_minute:
                # We must
                reply(u'warning|rate|%s' % next_minute_start)

        
    else:
        # This message is not supported by our chat protocol
        print 'Unsupported message: %s' % message  # For debugging purposes
        raise CloseConnectionException()


#######################################################################################################################
#
#    Third part, initialization of the server
#
#######################################################################################################################

if __name__ == '__main__':
    """
    Threads of this program, in the order they are spawned:
        - Main (1), used to wait for keyboard interrupts (CONTROL + C)
            - Server (1), used for the server socket that accepts new connections
                - Handle Connections (1), waits for new messages, calls appropriate handlers, and sends responses
    """
    def serve():
        # We create a server and start serving
        server = NanoWebServer(
            host='',
            port=80,
            http_handlers=[
                ('/', serve_frontend),
            ],
            ws_handlers=[
                ('/chat', on_chat_connection, serve_chat)
            ]
        )
        server.start()
    try:
        # We create a thread for the server, and use the main one
        # to listen for keyboard interrupts every second
        threading.Thread(target=serve).start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print "Bye!"
        os._exit(0)