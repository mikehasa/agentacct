import sys
import time

for _ in range(50):
    print('DUMMY_REPEAT_ERROR dependency exploded', file=sys.stderr, flush=True)
    time.sleep(0.05)
while True:
    time.sleep(1)
