#! /usr/bin/python3

import requests
import json
import subprocess
import sys

def main_ipcheck(xhomeip1):
    xcounter = 0
    xstatuscode = 0
    xrequest = ""
    xrequest_status = ""
    xhomeip = ""
    xrequest_status_state = "secure"
    xdig1 = "dig"
    xdig2 = "+short"
    xdig3 = "myip.opendns.com"
    xdig4 = "@resolver1.opendns.com"

    xproc = subprocess.Popen([xdig1, xdig2, xdig3, xdig4], stdout=subprocess.PIPE)
    xhomeip2 = xproc.stdout.read().decode('ascii')
    if xhomeip1.strip() == xhomeip2.strip():
        return 0
    else:
        return 1

def donothing():
    while xstatuscode != 200 and xcounter < 25:
          xcounter += 1
          xrequest = requests.get('https://ifconfig.co/json', timeout=1)
          xstatuscode = xrequest.status_code
          try:
               xrequest_status = xrequest.raise_for_status()
          except:
               xrequest_status_state = "failed"
    
    # print("Status code... ",xstatuscode)
    # print(xrequest.text)
    xrequest_text = xrequest.text
    # print("xrequest_text output")
    # print(xrequest_text)
    xrequest_parsed = json.loads(xrequest_text)
    # print(xrequest_parsed["ip"])
    # print(xrequest_parsed["country"])
    # print(xrequest_parsed["asn_org"])
    
    if ( (xrequest_parsed["asn_org"] == 'SHAW' and xrequest_parsed["country"] == 'Canada') or
            xcounter == 25 or xrequest_status == "failed"):
#        not secure
#        print("fail. count: ", xcounter, "code: ", xstatuscode, "status: ", xrequest_status)
        return 0
    else:
#        secure
#        print("secure")
        return 1

if __name__ == "__main__":
    xresult = main_ipcheck(sys.argv[1])
    if xresult == 1:
        print("secure")
    else:
        print("notsecure")
    quit()
