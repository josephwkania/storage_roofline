#!/bin/bash
#SBATCH --job-name=storage-roofline
#SBATCH --nodes=4
#SBATCH --ntasks-per-node=1
#SBATCH --exclusive
#SBATCH --time=0:31:00
#SBATCH --output=/path/to/results/storage_roofline_lustre_cluster_A_%j.log
#SBATCH --partition=compute

# storage-roofline: full profile, about 31 min.
# The multi-node part loads the whole filesystem: run it at a quiet
# time, with the agreement of the people who run the storage.
set -euo pipefail
/path/to/venv/bin/python -m storage_roofline run --dir /path/to/scratch --label 'lustre (cluster A)' --profile full --size 34359738368 --runtime 30 --fio /usr/bin/fio --files 20000 --python-import 'numpy scipy' \
    -o /path/to/results/lustre_cluster_A_${SLURM_JOB_ID}.json
