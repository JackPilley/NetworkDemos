import select
import socket
import sys
import re
import os
import time

#Matches the end of a request header
messageEndRegex = re.compile(r"\r?\n\r?\n")
#Matches an HTTP GET request and captures the file path
requestRegex = re.compile(r"^GET (?P<dir>/.*) HTTP/1\.0$", re.MULTILINE)
#Matches a request header for "connection: keep-alive", case insensitive and accounting for white space
keepAliveRegex = re.compile(r"^[^\S\r\n]*connection:[^\S\r\n]*keep-alive[^\S\r\n]*$", re.M|re.I)

homeDir = os.getcwd()

#print(homeDir)
#
#test = """GET / HTTP/1.0
#Connection: keep-alive
#
#GET / HTTP/1.0
#Connection: close
#
#"""
#
#matching = messageEndRegex.search(test)
#
#print(bytearray(test[:matching.start()].encode()))
#print(bytearray(test[matching.end():].encode()))

class ClientConnection:
    def __init__(self, sock):
        self.sock = sock
        peerInfo = sock.getpeername()
        self.peerInfo = peerInfo[0] + ":" + str(peerInfo[1])
        #Accumulates the data received from the client
        self.messageIn = ""
        #Buffer of data to send to the client
        self.messageOut = bytearray()
        #Should the connection be kept alive after the response to the first request
        self.keepAlive = False
        self.lastReadTime = time.time()
    
    #Returns the fileno of the internal socket.
    #For compatibility with select.select()
    def fileno(self):
        return self.sock.fileno()

    #Process inbound data from the client, returns true if there is data
    #to send to the client, false otherwise
    def processInboundData(self):
        #Update the last time the socket received data so it doesn't get
        #cleaned up from being idle
        self.lastReadTime = time.time()
        
        self.messageIn += self.sock.recv(4096).decode()

        #Check if we've hit the end of a request header
        endMatch = messageEndRegex.search(self.messageIn)
        
        requestHandled = False
        
        #Handle requests if there are any
        while endMatch:
            requestHandled = True
            # If there was a bad request or the connection isn't keep-alive, 
            # return immediately and don't process any other requests
            if not self.handleRequest(self.messageIn[:endMatch.start()]):
                return True
            self.messageIn = self.messageIn[endMatch.end():]
            #Check if there is another request (Might happen if the client
            #is sending a lot of requests close together)
            endMatch = messageEndRegex.search(self.messageIn)
            
        return requestHandled

    #Handle a client request and add our response to the messageOut buffer
    #Returns False if we should close the connection and True otherwise
    def handleRequest(self, request):
        lines = request.splitlines()
        firstLine = lines[0] if len(lines) > 0 else ""
        
        logline = time.strftime("%a %b %d %H:%M:%S %Z %Y")
        logline += ": " + self.peerInfo + " " + firstLine + "; "

        #Get the request line
        requestMatch = requestRegex.match(request)
        if requestMatch:
            #Determine if the client wants to keep the connection alive
            ka = ""
            if keepAliveRegex.search(request):
                self.keepAlive = True
                ka = "keep-alive"
            else:
                self.keepAlive = False
                ka = "close"

            #Get the requested filename from the request line
            filename = requestMatch.group('dir')
            try:
                file = open(homeDir+filename, "rb")

                #Read the file into a buffer first in case an exception is raised
                output = ("HTTP/1.0 200 OK\nConnection: " + ka + "\n\n").encode()
                output += file.read()
                #Once the file is read, add it to the output buffer
                self.messageOut += output

                logline += "HTTP/1.0 200 OK"

            #The error might not be that the file doesn't exist, but it's the most likely
            #so it's the answer we're just going to give for any file read errors
            except OSError as err:
                self.messageOut += ("HTTP/1.0 404 Not Found\nConnection: " + ka + "\n\n").encode()
                logline += "HTTP/1.0 404 Not Found"

            print(logline)

            return self.keepAlive

        else:
            self.messageOut += "HTTP/1.0 400 Bad Request\nConnection: close\n\n".encode()
            self.keepAlive = False

            logline += "HTTP/1.0 400 Bad Request"
            print(logline)

            return False
            
    #Send as much buffered data as possible. Returns True if there is more
    #data to send and False otherwise
    def sendData(self):
        totalSent = self.sock.send(self.messageOut)
        self.messageOut = self.messageOut[totalSent:]
        return len(self.messageOut) > 0

    #Properly shutdown and close the internal socket
    def close(self):
        self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()

def badArgsExit():
    print("Usage: python sws.py <ip address> <port number>")
    sys.exit()

if len(sys.argv) != 3:
    badArgsExit()

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setblocking(0)
server.bind((sys.argv[1], int(sys.argv[2])))
server.listen(5)

inbounds = [server]
outbounds = []
exceptable = []

while True:
    toRead, toWrite, errorThrown = select.select(inbounds, outbounds, exceptable, 10)
    
    #For each socket with data to read
    for s in toRead:
        if s is server:
            (sock, addr) = server.accept()
            cl = ClientConnection(sock)
            inbounds.append(cl)
            exceptable.append(cl)
        else:
            if s.processInboundData():
                inbounds.remove(s)
                outbounds.append(s)
    
    #For each socket that can send data (and has data to send)
    for s in toWrite:
        if not s.sendData():
            outbounds.remove(s)
            if s.keepAlive:
                inbounds.append(s)
            else:
                exceptable.remove(s)
                s.close()

    #Clean up any sockets that have thrown exceptions
    for s in errorThrown:
        inbounds.remove(s)
        outbounds.remove(s)
        exceptable.remove(s)
        s.close()

    #Clean up any sockets that have been idle for too long
    now = time.time()
    for s in inbounds:
        if s is not server:
            if now - s.lastReadTime > 10:
                s.close()
                inbounds.remove(s)
                exceptable.remove(s)
