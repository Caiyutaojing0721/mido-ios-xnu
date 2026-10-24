import subprocess, sys, os
ls = open('out/p98/osfmk/arm64/start.s', encoding='utf-8').read().split('\n')
blocks = []
i = 0
while i < len(ls):
    if 'Breadcrumb (' in ls[i]:
        beg = i - 1
        while ls[beg].strip() != '/*':
            beg -= 1
        end = i
        while ls[end].strip() != '1:':
            end += 1
        blocks.append(ls[beg:end + 1])
        i = end
    i += 1
print("found %d breadcrumb blocks" % len(blocks))
for b in blocks:
    print("  %d lines, first insn: %s" % (len(b), [l for l in b if 'ldr' in l][0].strip()))
src = ['.text', '.globl _t', '.p2align 2', '_t:']
for b in blocks:
    src += b
src += ['\tret']
open('out/crumb_check.s', 'w', newline='\n').write('\n'.join(src) + '\n')
PY ="C:/Users/才羽桃井/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
r = subprocess.run([PY, '-m', 'ziglang', 'cc', '-target', 'aarch64-freestanding',
                    '-c', '-o', 'out/crumb_check.o', 'out/crumb_check.s'],
                   capture_output=True, text=True)
errs = [l for l in (r.stdout + r.stderr).split('\n') if ' error:' in l or 'Error' in l]
print("assembler output:", (r.stdout + r.stderr).strip()[:600] or "(silent)")
print("object:", os.path.exists('out/crumb_check.o'))
print("RESULT:", "OK" if os.path.exists('out/crumb_check.o') and not errs else "FAIL")
