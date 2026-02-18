repos := "bitcoin-bitcoin bitcoin-core-gui bitcoin-core-secp256k1 bitcoin-bips"

# Generate all data files
data backup_dir:
    #!/usr/bin/env bash
    set -euo pipefail
    for repo in {{repos}}; do
        echo "Extracting $repo..."
        python3 extract_data.py "{{backup_dir}}/github-metadata-backup-$repo" \
            --username-map username_map.json \
            -o "data-$repo.json"
    done
