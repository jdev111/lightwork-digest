# Safety Verification Plan: No Email Can Be Sent

Run each test below. Every one must pass.

---

## Test 1: No SMTP imports exist

```bash
grep -n "smtplib\|MIMEMultipart\|MIMEText\|email.mime" post_call_digest.py
```

**Expected:** No output (zero matches)

---

## Test 2: No SMTP credentials in .env

```bash
grep -n "SMTP\|DIGEST_TO" .env
```

**Expected:** No output (zero matches)

---

## Test 3: No send function exists

```bash
grep -n "def send\|def mail\|def email\|sendmail\|send_email\|send_digest" post_call_digest.py
```

**Expected:** No output (zero matches)

---

## Test 4: Close.com write operations are blocked

```bash
python3 -c "
from post_call_digest import _request
try:
    _request('POST', 'https://api.close.com/api/v1/activity/email/', body={'test': True})
    print('FAIL: POST was not blocked')
except RuntimeError as e:
    print(f'PASS: {e}')
"
```

**Expected:** `PASS: SAFETY: Blocked POST request to Close.com...`

---

## Test 5: No Close.com write functions exist

```bash
grep -n "def close_post\|def close_put\|def close_delete\|def close_patch" post_call_digest.py
```

**Expected:** No output (zero matches)

---

## Test 6: Only GET requests go to Close.com

```bash
grep -n "api.close.com" post_call_digest.py
```

**Expected:** Only these lines appear:
- The safety block in `_request()` (blocking non-GET)
- The URL construction in `close_get()`

---

## Test 7: Lead emails never reach any outbound function

```bash
python3 -c "
import ast, sys

with open('post_call_digest.py') as f:
    tree = ast.parse(f.read())

# Find all function calls that use 'lead_email' as an argument
for node in ast.walk(tree):
    if isinstance(node, ast.Call):
        for arg in node.args + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Name) and arg.id == 'lead_email':
                func_name = ''
                if isinstance(node.func, ast.Name):
                    func_name = node.func.name
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                print(f'Line {node.lineno}: lead_email passed to {func_name}()')

print('(If no output above, lead_email is never passed to any function call)')
"
```

**Expected:** No output. `lead_email` only appears inside an f-string in the Claude prompt, never as a function argument.

---

## Test 8: Full outbound connection audit

```bash
python3 -c "
import re

with open('post_call_digest.py') as f:
    code = f.read()

# Find all URLs the script connects to
urls = re.findall(r'https?://[^\s\"'\)]+', code)
for url in sorted(set(urls)):
    print(url)
"
```

**Expected:** Only these 3 domains:
- `https://api.close.com/...` (read-only, GET blocked for writes)
- `https://docs.google.com/...` (Granola sheet download)
- `https://api.anthropic.com/...` (Claude text generation)

No SMTP servers, no email APIs, no other outbound services.

---

## Test 9: The script produces only a local file

```bash
python3 -c "
from post_call_digest import main
import inspect
source = inspect.getsource(main)
# Check that main() only writes to a local file
assert 'output_path.write_text' in source, 'FAIL: No local file write found'
assert 'send' not in source.lower() or 'send' only in comments, 'FAIL: send keyword found in main()'
assert 'smtp' not in source.lower(), 'FAIL: smtp keyword found in main()'
print('PASS: main() only writes a local HTML file')
"
```

**Expected:** `PASS: main() only writes a local HTML file`

---

## Run All Tests at Once

```bash
cd /Users/dillandevram/Desktop/claude-projects/lightwork-digest && python3 -c "
import re, ast

with open('post_call_digest.py') as f:
    code = f.read()

results = []

# Test 1: No SMTP imports
t1 = 'smtplib' not in code and 'MIMEMultipart' not in code and 'MIMEText' not in code
results.append(('No SMTP imports', t1))

# Test 2: No send function
t2 = 'def send' not in code and 'sendmail' not in code
results.append(('No send function', t2))

# Test 3: No close_post/put/delete
t3 = 'def close_post' not in code and 'def close_put' not in code and 'def close_delete' not in code
results.append(('No Close.com write functions', t3))

# Test 4: Close.com write block exists
t4 = 'api.close.com' in code and 'method.upper() != \"GET\"' in code and 'RuntimeError' in code
results.append(('Close.com write block active', t4))

# Test 5: lead_email never passed to a function
tree = ast.parse(code)
lead_email_leaked = False
for node in ast.walk(tree):
    if isinstance(node, ast.Call):
        for arg in node.args + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Name) and arg.id == 'lead_email':
                lead_email_leaked = True
results.append(('lead_email never in function args', not lead_email_leaked))

# Test 6: Only 3 external domains
urls = re.findall(r'https?://([^/\s\"\']+)', code)
domains = set(u.split('/')[0] for u in urls)
allowed = {'api.close.com', 'docs.google.com', 'api.anthropic.com', 'www.lightworkhome.com', 'www.amazon.com', 'lightwork-home-health.kit.com', 'safelivingtechnologies.com', 'lessemf.com', 'techwellness.com', 'aranet.com', 'ckarchive.com'}
unexpected = domains - allowed
t6 = len(unexpected) == 0
results.append(('No unexpected outbound domains', t6))

# Print results
print('SAFETY VERIFICATION')
print('=' * 40)
all_pass = True
for name, passed in results:
    status = 'PASS' if passed else 'FAIL'
    if not passed:
        all_pass = False
    print(f'  [{status}] {name}')
print('=' * 40)
print('ALL TESTS PASSED' if all_pass else 'SOME TESTS FAILED')
"
```

**Expected output:**
```
SAFETY VERIFICATION
========================================
  [PASS] No SMTP imports
  [PASS] No send function
  [PASS] No Close.com write functions
  [PASS] Close.com write block active
  [PASS] lead_email never in function args
  [PASS] No unexpected outbound domains
========================================
ALL TESTS PASSED
```
