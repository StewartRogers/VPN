#! /usr/bin/python3

import urllib.request
import sys

def main_ipcheck(xhomeip1):
    xhomeip2 = urllib.request.urlopen('https://api.ipify.org/').read().decode('utf8')
    if xhomeip1.strip() == xhomeip2.strip():
        return 0
    else:
        return 1

if __name__ == "__main__":
    xresult = main_ipcheck(sys.argv[1])
    if xresult == 1:
        print("secure")
    else:
        print("notsecure")
    quit()
