import sys
from pathlib import Path
from time import sleep

index = sys.argv[1]
Path(f"started-{index}").touch()
sleep(30)
Path(f"late-{index}").touch()
