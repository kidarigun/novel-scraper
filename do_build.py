import os
import sys
import unittest
from pathlib import Path
import PyInstaller.__main__

PROJECT_DIR = Path(__file__).resolve().parent
os.chdir(PROJECT_DIR)


print("[*] Running unit tests...")
loader = unittest.TestLoader()
suite = loader.discover(str(PROJECT_DIR), pattern="test_*.py")
runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)

if not result.wasSuccessful():
    print("[X] Unit tests failed!")
    sys.exit(1)

print(f"[+] Unit tests passed. Building single executable file in {PROJECT_DIR}...")


PyInstaller.__main__.run([
    '--noconfirm',
    '--clean',
    '--onefile',
    '--windowed',
    '--name=소설스크래퍼',
    '--collect-all=scrapling',
    '--collect-all=patchright',
    '--collect-all=browserforge',
    '--collect-all=apify_fingerprint_datapoints',
    '--collect-all=curl_cffi',
    '--collect-all=babel',
    '--collect-all=tldextract',
    'gui.py'
])
print("[+] Build process finished successfully!")
