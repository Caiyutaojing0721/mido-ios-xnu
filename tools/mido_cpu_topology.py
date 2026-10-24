#!/usr/bin/env python3
"""Fail-closed mido (Redmi Note 4X / MSM8953) CPU prefix check.

Validates the serialized Apple Device Tree, not a claim that CPU_ON succeeds.
mido is 8x Cortex-A53 arranged as TWO clusters of four (both A53, so there is no
asymmetric core to exclude). The expected MPIDRs are taken from the device's own
DTB -- 0,1,2,3 then 0x100,0x101,0x102,0x103 -- not the 0x00/0x100/../0x700
spread used for the polaris tree. The check is the eight-node identity plus a
contiguous cpus=1..8 boot-argument prefix.
"""

import argparse
import struct

from adt import load, parse

# MPIDR per logical CPU, measured from /cpus/cpu@* in the mido boot.img DTB.
MIDO_MPIDR = (0x000, 0x001, 0x002, 0x003, 0x100, 0x101, 0x102, 0x103)
CLUSTER_SIZE = 4


class TopologyError(ValueError):
    pass


def _property(node, name):
    value = node.prop(name)
    if value is None or len(value) != 4:
        raise TopologyError(f"{node.name}: missing/invalid {name}")
    return struct.unpack('<I', value)[0]


def _child(parent, name):
    matches = [child for child in parent.children if child.name == name]
    if len(matches) != 1:
        raise TopologyError(f"expected exactly one {name} node")
    return matches[0]


def validate(adt_bytes, cmdline):
    """Return the selected contiguous prefix, refusing ambiguous CPU boot args."""
    tree, end = parse(adt_bytes)
    if end != len(adt_bytes):
        raise TopologyError('trailing ADT bytes')
    tokens = cmdline.split()
    selections = [t for t in tokens if t.startswith('cpus=')]
    if len(selections) != 1 or any(t.startswith('cpumask=') for t in tokens):
        raise TopologyError('supply exactly one cpus=N, without cpumask')
    try:
        count = int(selections[0][5:])
    except ValueError as exc:
        raise TopologyError('invalid cpus=N') from exc
    if not 1 <= count <= 8:
        raise TopologyError('mido cpus must be in 1..8')
    chosen = _child(tree, 'chosen')
    compatible = tree.prop('compatible')
    if not compatible or b'xiaomi,mido\0' not in compatible or b'qcom,msm8953\0' not in compatible:
        raise TopologyError('not a mido/MSM8953 ADT')
    cpus = _child(tree, 'cpus')
    if len(cpus.children) != 8:
        raise TopologyError('mido ADT must describe eight CPUs')
    for index, cpu in enumerate(cpus.children):
        if cpu.name != f'cpu{index}' or _property(cpu, 'cpu-id') != index:
            raise TopologyError(f'CPU {index} node name / cpu-id mismatch')
        if _property(cpu, 'reg') != MIDO_MPIDR[index]:
            raise TopologyError(
                f'CPU {index} MPIDR mismatch: expected 0x{MIDO_MPIDR[index]:x}')
        if _property(cpu, 'cluster-id') != index // CLUSTER_SIZE:
            raise TopologyError(f'CPU {index} cluster-id mismatch')
        if _property(cpu, 'cluster-core-id') != index % CLUSTER_SIZE:
            raise TopologyError(f'CPU {index} cluster-core-id mismatch')
        # "arm,cortex-a53" is what the silicon is; "qcom,kryo470" is what
        # SweetCPU's IONameMatch (patch 0018) requires. Both must be present or
        # the CPU driver never instantiates -- the boot hangs with no CPU nodes.
        compat = cpu.prop('compatible') or b''
        if b'arm,cortex-a53\0' not in compat:
            raise TopologyError(f'CPU {index} is not an A53')
        if b'qcom,kryo470\0' not in compat:
            raise TopologyError(f'CPU {index} lacks the qcom,kryo470 match string')
        if cpu.prop('state') != (b'running\0' if index == 0 else b'stopped\0'):
            raise TopologyError(f'CPU {index} state mismatch')
    if not chosen.prop('name'):
        raise TopologyError('missing chosen name')
    return count


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('adt')
    ap.add_argument('--cmdline', required=True)
    args = ap.parse_args()
    try:
        count = validate(load(args.adt), args.cmdline)
    except (TopologyError, ValueError, IndexError, struct.error) as exc:
        ap.error(str(exc))
    print(f'mido ADT prefix: {count}/8 selected (all A53; CPU_ON NOT phone-verified)')


if __name__ == '__main__':
    main()
