Shout!
======

**A simple chat or "shoutbox" based on WebSockets, pure Javascript, and Python.**


![Screenshot](/Screenshot.png "Screenshot showcasing some of the features")


Simple and lightweight
----------------------
No images; No CSS classes; no Javascript framework; no backend dependencies. Pure HTML, CSS3, Javascript and Python.
Requires a modern browser (particularly IE10+).

Beautiful and full-featured
---------------------------
It adapts to a fluid design, allows username selection (handling username collisions), provides rate limiting and spam control (users can ignore particular commenters), etc.

Versatile
---------
The frontend can be easily adapted to your preferred Javascript framework, including jQuery. The lack of CSS classes means there will be minimum interference with your existing stylesheets.

The backend is **a custom, multi-threaded, asynchronous (non-blocking) HTTP/WebSockets server** ([RFC-2616](https://tools.ietf.org/html/rfc2616)/[RFC-6455](https://tools.ietf.org/html/rfc6455)) written in pure Python. It's however very limited and untested, and should be ported to a real server for production use. It can also be adapted to your programming language of choice using the original code as a guide.

Educational
-----------
I've developed Shout! to learn about WebSockets and low-level networking in Python, and I've commented the code to guide you through my steps.

For your convenience the entire project is contained in only two files: a [.html](/shout.html) for the frontend and a [.py](shout.py) for the backend.

MIT-Licensed
------------
Feel free to use, edit, or distribute this code in any way you want. Check out the [GitHub repository](https://github.com/jsantirso/shout/); your contributions will be more than welcome.

Usage
=====
- Download shout.html and shout.py and place them in the same folder
- Execute shout.py
- Navigate to /localhost in your preferred browser(s)
