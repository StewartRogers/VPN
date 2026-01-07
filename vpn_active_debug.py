
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
#! /usr/bin/python3

import urllib.request
import sys
import requests

def main_ipcheck(xhomeip1):
    try:
      xhomeipR = requests.get("https://api.ipify.org?format=json", timeout=5)
      xhomeip2 = xhomeipR.json().get("ip")
#      xhomeip2 = urllib.request.urlopen('https://api.ipify.org/').read().decode('utf8')
      print("xhomeip1: ", xhomeip1.strip())
      print("xhomeip2: ", xhomeip2.strip())
      if xhomeip1.strip() == xhomeip2.strip():
         return 0
      else:
         return 1
    except requests.RequestException:
         return 0


if __name__ == "__main__":
    xresult = main_ipcheck(sys.argv[1])
    if xresult == 1:
        print("secure")
    else:
        print("notsecure")
    quit()
