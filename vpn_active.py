#! /usr/bin/python3

import requests
import json

# print "Running... requests"
x2 = 0
r1 = ""
r2 = 0
r3 = ""
r4 = "secure"

while r2 != 200 and x2 < 25:
      x2 += 1
      r1 = requests.get('https://ifconfig.co/json', timeout=10)
      r2 = r1.status_code
      try:
           r3 = r1.raise_for_status()
      except:
           r4 = "failed"

# print("Status code... ",r2)
# print "Out while..."
# print r1.text
J1 = r1.text
# print "J1 output"
# print J1
parseX = json.loads(J1)
# print parseX["country"]
# print parseX["asn_org"]

if ( ( parseX["asn_org"] == 'SHAW' and parseX["country"] == 'Canada' ) or x2 == 25 or r3 == "failed" ):
  print("false... count... ", x2, "code...", r2, "status... ", r3)
else:
  print("secure")
quit()
