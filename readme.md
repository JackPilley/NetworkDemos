# Network Demos
Some simple networking demonstrations:
* sws.py is a simple web server that can handle HTTP get requests.
* rdp.py is a example implementation of a reliable transport protocol built on UDP. It sends data to an echo server and then constructs a duplicate of the original file using the received data. It works over unstable connections and corrects for lost packets.