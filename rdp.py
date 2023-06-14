from abc import ABC, abstractmethod
from enum import Enum, auto
from nis import match
import os
import queue
from select import select
import select
import socket
import sys
import re
import time
#Used for testing
#import threading

headerEndRegex = re.compile(b"\r?\n\r?\n")

lenRegex = re.compile(r"Length: ?(?P<len>\d+)$")
seqRegex = re.compile(r"Sequence: ?(?P<seq>\d+)$")
ackRegex = re.compile(r"Acknowledgment: ?(?P<ack>\d+)$")
winRegex = re.compile(r"Window: ?(?P<win>\d+)$")

# File that is sent over the network
outboundFile = open(sys.argv[3], "rb")
# File to save received network data
inboundFile = open(sys.argv[4], "wb")


class RDPHeader:
    def __init__(self):
        self.command = ""
        self.length = 0
        self.sequence = 0
        self.ack = 0
        self.window = 0

class RDPState(Enum):
    # Class instance was just created
    IDLE = auto()
    # Synchronizing
    SYNC = auto()
    # The receiver is receiving data
    RECEIVING = auto()
    # The sender is sending data
    SENDING = auto()
    # The endpoint is confirming that all data has been sent/received
    FINISHING = auto()
    # The endpoint has completed its task
    DONE = auto()

# Base class for RDP endpoints
class RDPEndPoint(ABC):
    def __init__(self, sock, addr):
        self.MAX_PAYLOAD = 1024
        
        self.sock = sock
        self.addr = addr
        self.state = RDPState.IDLE
        # How long to wait until we try sending the last message again
        self.retryDelay = 0.5
        # When to retry sending to the other endpoint
        self.retryTime = time.time() + self.retryDelay
        # Derrived class dependent
        self.ack = 0
        self.windowSize = 2048
    
    #Compatibility with select.select()
    def fileno(self):
        return self.sock.fileno()
    
    @abstractmethod
    def recv(self, header, payload):
        pass

    @abstractmethod
    def send(self):
        pass

class Receiver(RDPEndPoint):
    def __init__(self, sock, addr, fileOut):
        super().__init__(sock, addr)
        self.fileOut = fileOut
        self.window = bytearray()
        # Byte number of the first byte in the window
        self.firstByteIndex = 0
        # Most recent byte we've sent an ack for
        self.lastAck = 0
        # Note: self.ack is used to represent the byte number we should
        # acknowledge (i.e. the largest byte number that is conitguous
        # with the start of the window)

        self.sendFinAck = False

    def recv(self, header, payload):
        logline = time.strftime("%a %b %d %H:%M:%S %Z %Y")
        logline += f": Receive; {header.command}; Sequence: {header.sequence}; Length: {header.length}"
        print(logline)
        if self.state == RDPState.IDLE:
            if header.command == "SYN":
                self.retryTime = time.time() + self.retryDelay
                self.ack = header.sequence + 1
                self.state = RDPState.SYNC
        elif self.state == RDPState.RECEIVING:
            if header.command == "DAT":
                if header.sequence == self.ack:
                    self.retryTime = time.time() + self.retryDelay
                    self.ack = header.sequence + header.length
                    self.window += payload[:min(len(payload), self.windowSize - len(self.window))]
                    written = self.fileOut.write(self.window)
                    self.window = self.window[written:]
            elif header.command == "FIN":
                self.state = RDPState.FINISHING
                self.ack = header.sequence + header.length
                # If we go beyond this time wihout receiving any message
                # from the sender then we'll assume it received our ACK
                self.retryTime = time.time() + self.retryDelay * 5
                self.sendFinAck = True
                self.fileOut.close()
        elif self.state == RDPState.FINISHING:
            if header.command == "FIN":
                self.retryTime = time.time() + self.retryDelay * 5
                self.sendFinAck = True

    def send(self):
        logline = time.strftime("%a %b %d %H:%M:%S %Z %Y")
        if self.state == RDPState.SYNC:
            logline += f": Send; ACK; Acknowledgment: {self.ack}; Window: {self.windowSize - len(self.window)}"
            print(logline)
            self.state = RDPState.RECEIVING
            self.sock.sendto(f"ACK\nAcknowledgment: {self.ack}\nWindow: {self.windowSize - len(self.window)}\n\n".encode(), self.addr)
            self.lastAck = self.ack
        elif self.state == RDPState.RECEIVING:
            if self.ack != self.lastAck or time.time() > self.retryTime:
                logline += f": Send; ACK; Acknowledgment: {self.ack}; Window: {self.windowSize - len(self.window)}"
                print(logline)
                self.retryTime = time.time() + self.retryDelay
                self.sock.sendto(f"ACK\nAcknowledgment: {self.ack}\nWindow: {self.windowSize - len(self.window)}\n\n".encode(), self.addr)
                self.lastAck = self.ack
        elif self.state == RDPState.FINISHING:
            if self.sendFinAck:
                logline += f": Send; ACK; Acknowledgment: {self.ack}; Window: {self.windowSize - len(self.window)}"
                print(logline)
                self.sendFinAck = False
                self.sock.sendto(f"ACK\nAcknowledgment: {self.ack}\nWindow: {self.windowSize - len(self.window)}\n\n".encode(), self.addr)
            elif time.time() > self.retryTime:
                self.state = RDPState.DONE


class Sender(RDPEndPoint):
    def __init__(self, sock, addr, fileOut):
        super().__init__(sock, addr)
        # The ack number from the receiver
        self.lastAck = 0
        #Last sequence start that we sent
        self.ack = 0
        self.fileOut = fileOut
        self.outData = fileOut.read(4096)
        self.fileOutSize = os.path.getsize(fileOut.name)
        # Sequence number of the first byte sent
        self.startingSeq = self.ack + 1

    def recv(self, header, payload):
        logline = time.strftime("%a %b %d %H:%M:%S %Z %Y")
        if header.command == "ACK":
            logline += f": Receive; {header.command}; Acknowledgment: {header.ack}; Window: {header.window}"
            print(logline)
            if self.state == RDPState.SYNC:
                if header.ack == self.startingSeq:
                    self.retryTime = time.time() + self.retryDelay
                    self.lastAck = header.ack
                    self.state = RDPState.SENDING
            elif self.state == RDPState.SENDING:
                if header.ack == self.fileOutSize + self.startingSeq:
                    self.state = RDPState.FINISHING
                    # Increment the next sequence number to avoid old ACK packets being confused the FIN acknowledgement
                    self.lastAck = header.ack + 1
                elif header.ack > self.lastAck:
                    self.retryTime = time.time() + self.retryDelay
                    self.windowSize = header.window
                    self.outData = self.outData[header.ack - self.lastAck:] + self.fileOut.read(header.ack - self.lastAck)

                    self.lastAck = header.ack

            elif self.state == RDPState.FINISHING:
                if header.ack > self.fileOutSize + self.startingSeq:
                    self.state = RDPState.DONE

    def send(self):
        logline = time.strftime("%a %b %d %H:%M:%S %Z %Y")
        logline += ": Send; "

        if self.state == RDPState.IDLE:
            logline += "SYN; Sequence: 0; Length: 0"
            self.sock.sendto(f"SYN\nSequence: {self.ack}\nLength: 0\n\n".encode(), self.addr)
            self.state = RDPState.SYNC
            print(logline)
        elif self.state == RDPState.SYNC:
            if time.time() > self.retryTime:
                logline += "SYN; Sequence: 0; Length: 0"
                self.retryTime = time.time() + self.retryDelay
                self.sock.sendto(f"SYN\nSequence: {self.ack}\nLength: 0\n\n".encode(), self.addr)
                print(logline)
        elif self.state == RDPState.SENDING:
            if self.lastAck > self.ack or time.time() > self.retryTime:
                self.retryTime = time.time() + self.retryDelay
                
                endIndex = min(self.MAX_PAYLOAD, min(self.windowSize, len(self.outData)))
                previous = 0
                while endIndex - previous > 0:
                    dataToSend = self.outData[previous:endIndex]
                    length = len(dataToSend)
                    logline += f"DAT; Sequence: {self.lastAck + previous}; Length: {length}"
                    packet = f"DAT\nSequence: {self.lastAck + previous}\nLength: {length}\n\n".encode()
                    packet += dataToSend
                    self.sock.sendto(packet, self.addr)

                    print(logline)
                    logline = time.strftime("%a %b %d %H:%M:%S %Z %Y")
                    logline += ": Send; "

                    previous = endIndex
                    endIndex += min(self.windowSize, self.MAX_PAYLOAD)
                    endIndex = min(self.windowSize, min(endIndex, len(self.outData)))

                self.ack = self.lastAck

        elif self.state == RDPState.FINISHING:
            if self.lastAck > self.ack or time.time() > self.retryTime:
                self.retryTime = time.time() + self.retryDelay
                self.ack = self.lastAck
                logline += f"FIN; Sequence: {self.lastAck}; Length: 0"
                self.sock.sendto(f"FIN\nSequence: {self.lastAck}\nLength: 0\n\n".encode(), self.addr)
                print(logline)

class DispatcherState(Enum):
    HEADER = auto()
    PAYLOAD = auto()

# The dispatcher accumulates incoming data, processes the header,
# and then dispatches it to either the sender or the receiver as appropriate
class RDPDispatcher:
    def __init__(self, sender, receiver):
        # Buffer to store accumulated data
        self.buffer = bytearray()
        # The disptcher alternates between two states, HEADER and PAYLOAD.
        # The state indicates what the dispatcher is currently accumulating.
        # Once the payload is complete, the data is dispatched and the state
        # resets to HEADER.
        self.state = DispatcherState.HEADER
        self.header = RDPHeader()
        self.payload = bytearray()

        self.sender = sender
        self.receiver = receiver

    def accumulate(self, data):
        self.buffer += data
        # While there is data to process.
        # The loop may terminate early if we only have part of a header
        while len(self.buffer) > 0:
            if self.state == DispatcherState.HEADER:
                headerEnd = headerEndRegex.search(self.buffer)
                # If we find the end of the header, fill out a header class and
                # switch to processing the payload
                if headerEnd:
                    self.fillHeader((self.buffer[:headerEnd.start()]).decode().splitlines())
                    self.buffer = self.buffer[headerEnd.end():]
                    self.state = DispatcherState.PAYLOAD
                else:
                    # The only remaining data is a partial header, so break and wait
                    # for the rest of the data
                    break
            
            # Intentionally not and elif, the state might have changed while processing
            # the header
            if self.state == DispatcherState.PAYLOAD:
                remainingData = self.header.length - len(self.payload)
                # If we have all of the data for the payload then dispatch it and
                # switch to processing the header again
                if len(self.buffer) >= remainingData:
                    self.payload += self.buffer[:remainingData]
                    self.buffer = self.buffer[remainingData:]
                    self.dispatch()
                    self.payload.clear()
                    self.state = DispatcherState.HEADER
                else:
                    self.payload += self.buffer
                    self.buffer.clear()

    #Fill a header class with the received information
    def fillHeader(self, header):
        self.header = RDPHeader()
        self.header.command = header[0]

        if header[0] == "SYN" or header[0] == "DAT" or header[0] == "FIN":
            for opt in header:
                lenMatch = lenRegex.match(opt)
                seqMatch = seqRegex.match(opt)
                if lenMatch:
                    self.header.length = int(lenMatch.group("len"))
                elif seqMatch:
                    self.header.sequence = int(seqMatch.group("seq"))

        elif header[0] == "ACK":
            for opt in header:
                ackMatch = ackRegex.match(opt)
                winMatch = winRegex.match(opt)
                if ackMatch:
                    self.header.ack = int(ackMatch.group("ack"))
                elif winMatch:
                    self.header.window = int(winMatch.group("win"))

    #Send the header and payload to the sender or receiver as appropriate
    def dispatch(self):
        command = self.header.command
        if command == "SYN" or command == "DAT" or command == "FIN":
            self.receiver.recv(self.header, self.payload)
        elif command == "ACK":
            self.sender.recv(self.header, self.payload)


sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

port = int(sys.argv[2])

addr = ("h2", 8888)

sock.setblocking(0)
sock.bind((sys.argv[1], port))

sender = Sender(sock, addr, outboundFile)
receiver = Receiver(sock, addr, inboundFile)

rdpdispatcher = RDPDispatcher(sender, receiver)

inbounds = [sock]
outbounds = [sender, receiver]
exceptable = []

retryTime = 0.1

#Used for testing
#def test():
#    sock.sendto("SYN\nSequence: 0\nLength: 0\n\n".encode(), ("h2", port))
#    sock.sendto("DAT\nSequence: 0\nLength: 3\n\nabc".encode(), ("h2", port))
#    sock.sendto("DAT\nSequence: 0\nLength: 3\n\ndef".encode(), ("h2", port))
#    sock.sendto("FIN\nSequence: 0\nLength: 0\n\n".encode(), ("h2", port))
#threading.Thread(target = test).start()

while True:
    toRead, toWrite, errorThrown = select.select(inbounds, outbounds, exceptable, retryTime)

    for s in toRead:
        data, origin = sock.recvfrom(8192)
        rdpdispatcher.accumulate(data)

    for s in toWrite:
        s.send()

    if sender.state == RDPState.DONE and receiver.state == RDPState.DONE:
        break