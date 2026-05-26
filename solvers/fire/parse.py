#!/usr/bin/env python3
"""
Simple parser for FIRE solver output.
Extracts reduction results from FIRE .tables files.
"""

import re
import json
from pathlib import Path


def parse_fire_output(output_dir: Path) -> dict:
    """Parse FIRE output directory and extract results."""

    # Find .tables file
    tables_files = list(output_dir.glob("*.tables"))
    if not tables_files:
        raise FileNotFoundError(f"No .tables file found in {output_dir}")

    tables_path = tables_files[0]

    # Extract topology and integral from directory name
    # Format: output_TIMESTAMP_TOPOLOGY_fire_INTEGRAL
    # TIMESTAMP format: YYYYMMDD_HHMMSS_MICROSECONDS
    dir_name = output_dir.name
    parts = dir_name.split('_')

    # Find topology (between timestamp and "fire")
    # Skip first 4 parts: "output", "YYYYMMDD", "HHMMSS", "MICROSECONDS"
    fire_idx = parts.index("fire")
    topology = "_".join(parts[4:fire_idx])

    # Integral indices are after "fire"
    integral_str = "_".join(parts[fire_idx + 1:])
    target_integral = tuple(map(int, integral_str.split('_')))

    # Read config to get parameters
    config_files = list(output_dir.glob("*.config"))
    if not config_files:
        raise FileNotFoundError(f"No .config file found in {output_dir}")

    # Parse config to extract parameters
    with open(config_files[0], 'r') as f:
        config_content = f.read()

    # Extract parameters from #variables line
    # Format: #variables d->6599,m1sq->4356,spkk->47
    params = {}
    vars_match = re.search(r'#variables\s+(.+)', config_content)
    if vars_match:
        vars_str = vars_match.group(1).strip()
        for param_pair in vars_str.split(','):
            if '->' in param_pair:
                key, val = param_pair.split('->', 1)
                key = key.strip()
                val = val.strip()
                params[key] = int(val)

    # Find log file for num_steps and finite-field prime
    log_files = list(output_dir.glob("*_fire_log.txt"))
    num_steps = None
    field_order = None
    if log_files:
        with open(log_files[0], 'r') as f:
            log_content = f.read()
        # Extract number of equations used
        # Pattern: "Eqs (total/used): 2631 | 2172"
        steps_match = re.search(r'Eqs \(total/used\):\s*(\d+)\s*\|\s*(\d+)', log_content)
        if steps_match:
            num_steps = int(steps_match.group(2))  # Use the "used" count
        # Extract the actual prime used for modular arithmetic (finite field order)
        # Pattern: "Prime number is: 2017"
        prime_match = re.search(r'Prime number is:\s*(\d+)', log_content)
        if prime_match:
            field_order = int(prime_match.group(1))

    # Parse .tables file
    with open(tables_path, 'r') as f:
        tables_content = f.read()

    # Extract mapping from integral_id to indices
    # Pattern: {integral_id, {0, {i1, i2, i3, i4, i5}}}
    id_to_indices = {}
    indices_pattern = r'\{(\d+),\s*\{0,\s*\{([^}]+)\}\}\}'
    for match in re.finditer(indices_pattern, tables_content):
        integral_id = match.group(1)
        indices_str = match.group(2).replace(' ', '')
        indices = [int(x) for x in indices_str.split(',')]
        id_to_indices[integral_id] = indices

    # Split the tables file into two main sections
    # Remove outer braces
    tables_stripped = tables_content.strip()
    if tables_stripped.startswith('{') and tables_stripped.endswith('}'):
        tables_stripped = tables_stripped[1:-1].strip()

    # Find the comma at depth 0 that separates the two main sections
    depth = 0
    split_pos = -1
    for i, char in enumerate(tables_stripped):
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
        elif char == ',' and depth == 0:
            split_pos = i
            break

    if split_pos < 0:
        raise ValueError("Could not split tables file into two sections")

    results_section = tables_stripped[:split_pos].strip()
    # Remove outer braces from results section if present
    if results_section.startswith('{') and results_section.endswith('}'):
        results_section = results_section[1:-1].strip()

    # Find the target integral's ID
    target_integral_list = list(target_integral)
    target_integral_id = None
    for integral_id, indices in id_to_indices.items():
        if indices == target_integral_list:
            target_integral_id = integral_id
            break

    if not target_integral_id:
        raise ValueError(f"Could not find target integral {target_integral_list} in tables")

    # Extract coefficients for target integral
    # Pattern: {integral_id, { {master_id, "coeff"}, ... }}
    master_coeffs = {}
    integral_block_pattern = rf'\{{{target_integral_id},\s*\{{((?:[^{{}}]|\{{[^{{}}]*\}})*)\}}\s*\}}'
    block_match = re.search(integral_block_pattern, results_section, re.DOTALL)

    if block_match:
        coeffs_block = block_match.group(1)

        # Parse master integral coefficients
        # Pattern: {master_id, "coefficient"}
        master_pattern = r'\{(\d+),\s*"(\d+)"\}'
        masters = re.findall(master_pattern, coeffs_block)

        for master_id, coeff in masters:
            # Get indices for this master
            if master_id in id_to_indices:
                master_indices = id_to_indices[master_id]
                master_coeffs[str(master_indices)] = int(coeff)

    master_coeffs = dict(sorted(master_coeffs.items(), key=lambda kv: json.loads(kv[0])))

    result = {
        "solver": "fire",
        "topology": topology,
        "finite_field": field_order,
        "params": params,
        "integrals": [target_integral_list],
        "reductions": {
            str(target_integral_list): master_coeffs
        },
        "num_steps": num_steps
    }

    return result


def main():
    import sys
    if len(sys.argv) < 2:
        print("Usage: python parse_fire.py <output_directory>")
        sys.exit(1)

    output_dir = Path(sys.argv[1])
    result = parse_fire_output(output_dir)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
