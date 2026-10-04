"""Check combo isolation and key positions before building this Corne config."""
import argparse
import re
from pathlib import Path


def check(path):
    text = re.sub(r'/\*.*?\*/|//[^\n]*', '', path.read_text(), flags=re.S)
    layer_nodes = re.findall(r'(\w+)\s*\{([^{}]*bindings\s*=\s*<[^{}]*)\}', text.split('    keymap {', 1)[1])
    combos = []
    for name, body in re.findall(r'(\w+)\s*\{([^{}]*key-positions[^{}]*)\}', text):
        positions = list(map(int, re.search(r'key-positions\s*=\s*<([^>]+)>', body)[1].split()))
        match = re.search(r'layers\s*=\s*<([^>]+)>', body)
        layers = set(map(int, match[1].split())) if match else set(range(len(layer_nodes)))
        if len(layer_nodes) > 3 and not match:
            raise ValueError(f'{name}: scope this global combo before adding another mode')
        if len(positions) != len(set(positions)) or any(p < 0 or p >= 48 for p in positions):
            raise ValueError(f'{name}: invalid physical positions {positions}')
        binding = re.search(r'bindings\s*=\s*<([^>]+)>', body)[1].strip()
        combos.append((name, frozenset(positions), layers, binding))
    for i, (name, positions, layers, _) in enumerate(combos):
        for other, other_positions, other_layers, _ in combos[i + 1:]:
            if positions == other_positions and layers & other_layers:
                raise ValueError(f'{name} and {other}: identical triggers on overlapping layers')
    layers = layer_nodes
    for name, body in layers:
        bindings = re.search(r'bindings\s*=\s*<([^>]+)>', body, re.S)[1]
        count = len(re.findall(r'&\w+', bindings))
        if count != 48:
            raise ValueError(f'{name}: {count} bindings, expected 48')
    if not layers or any(any(layer >= len(layers) or layer < 0 for layer in item[2]) for item in combos):
        raise ValueError('Combo references a nonexistent layer')
    print(f'PASS: {len(layers)} layers, 48 bindings each; {len(combos)} combos; no duplicate triggers.')
    print('Static validation only: compile and hardware timing still require verification.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('keymap', nargs='?', type=Path, default=Path(__file__).resolve().parents[1] / 'config/eyelash_corne.keymap')
    args = parser.parse_args()
    check(args.keymap)
