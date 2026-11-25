# storage-roofline

A roofline-style benchmark for filesystems. It measures where a filesystem's
limits are, the same way on any Linux cluster, and plots any number of
results together so filesystems and clusters can be compared.

- **Latency** (the diagonal): with a fixed time per request, throughput is
  data in flight ÷ latency (Little's law). On a log-log plot that is a
  diagonal line; slower storage sits further right.
- **Client roof**: the fastest one node can read, usually its network link.
- **Cluster roof**: the total when several nodes read at once, shared by every
  user of the filesystem.
- **Small files**: create, stat, open+read and unlink rates. This limit applies
  to software start-up and other many-small-file work, and bandwidth doesn't
  describe it.

The x-axis is **data in flight per client**: request size × queue depth ×
streams (for buffered reads, the readahead window × streams). That puts
request size, queue depth and readahead on one scale, so you can see which
lever helps. For example, a client sitting on the diagonal well below its roof
gets faster with more readahead; one at the roof needs a faster network.

## Install

Needs Linux, Python ≥ 3.9 and [fio](https://github.com/axboe/fio).

```bash
pip install "storage-roofline[plot] @ git+https://github.com/josephwkania/storage_roofline"
# fio, if your system doesn't have it:
conda install -c conda-forge fio      # or: apt/dnf install fio
```

Plotting needs the `[plot]` extra (numpy, matplotlib). Running the tests only
needs fio and the standard library, so compute nodes need nothing else.

## Run

**With Slurm** (recommended). This writes a batch script that asks for whole
nodes (`--exclusive`) so other jobs don't skew the numbers:

```bash
storage-roofline slurm --dir /path/on/the/fs/scratch --nodes 4 --partition main \
    --label "lustre (cluster A)"            # prints the script to review
storage-roofline slurm ... --submit         # submits it
```

With more than one node you also get the aggregate (cluster) read. The
small-file test is always **cold** in Slurm: the files are created on one node
and read on another, so the reading client has nothing cached. With
`--nodes 1` the script therefore asks for a second node, used only for that
read (`--warm-metadata` keeps it to one node, with warm small-file results).

**Without Slurm**, on one machine:

```bash
storage-roofline run --dir /path/on/the/fs/scratch --profile quick --label "nvme (laptop)"
```

The single-node part runs and the small-file test is marked **warm**.

Useful options:

| Option | Default | |
|---|---|---|
| `--profile quick\|full` | full | about 10 or 20 tests |
| `--size` | 32G | test file per node; larger than any cache in front of the storage |
| `--runtime` | 30 | seconds per test |
| `--files` | 20000 | small files (0 skips the test) |
| `--python-import "numpy scipy"` | | also time a fresh Python importing these modules |
| `--readahead 8M` | from sysfs | if it can't be read, buffered reads are left off the plot |
| `--read-once` | off | each read test reads its own part of the file once (see below) |
| `--nodes` (slurm) | 4 | 1: bandwidth on one node (plus a second node for the cold small-file read) |

A full run on 4 nodes takes about half an hour; `slurm` estimates the time
limit for you.

## Compare

```bash
storage-roofline plot cephfs.json lustre.json nfs.json -o roofline.png
storage-roofline summary cephfs.json lustre.json nfs.json
```

`plot` writes `roofline.png`, `roofline_metadata.png` (small files) and
`roofline_iops.png`, the same points as IOPS against operations per byte, the
axes of Zhu et al.'s I/O roofline (see Related work).
Each result gets a colour, its fitted latency line (dashed), its client roof
(solid) and, if measured, its cluster roof (dotted). `summary` prints the
headline numbers side by side.

## What it runs

| Part | Tests |
|---|---|
| Direct random reads | 4 KiB at queue depth 1/4/16/64; 64 KiB at QD16; 1 MiB at QD1/16 |
| Direct sequential reads | 4 MiB at QD 1/2/4/8/16/32 |
| Buffered sequential reads | 1/2/4/8/16 streams, page cache dropped first (each region read once) |
| Writes | 4 KiB random at QD16; 4 MiB sequential at QD16 |
| Aggregate read (≥ 2 nodes) | 1, 2, 4 … N nodes at once, each 4 MiB direct QD16 on its own file |
| Small files | create here, then stat, open+read and unlink (on another node if there is one) |

Direct I/O (`O_DIRECT`, libaio) bypasses the page cache; where it isn't
supported (e.g. tmpfs) only the buffered reads run. Dropping a file from the
page cache uses `posix_fadvise`, so no root is needed.

**Caches on the storage side.** Dropping the client's cache doesn't empty a
server's memory: an NFS server with lots of RAM, or a storage cache, can hold
the whole test file, and timed tests read it several times. `--read-once`
gives every read test its own part of the file and reads it at most once, so
only the first pass counts; use a `--size` of several GiB per read test. Writes
to NFS exports set to `async` are acknowledged before they reach disk, which no
client option changes. Results record these limits as notes, shown under the
plot and in `summary`.

The **cluster roof** is a lower bound: the best total with the nodes you used.
The aggregate is the sum of the nodes' rates while they run together; the
result also records how far apart their starts were.

## Results

One JSON file per run (`schema: 1`). Besides the measurements it records what
was measured, so results can be compared fairly: host name, Slurm cluster and
job ID, kernel, filesystem type, mount point and options (passwords and keys
removed), Ceph or Lustre layout, readahead, network link speeds, CPU, memory,
fio and Python versions. It doesn't record user names or your directory
path. Check a result before publishing it if the host or mount names matter.

## Be considerate

The tests write and read tens of GiB, and the multi-node part loads the whole
filesystem on purpose. On shared systems, run them at quiet times and with
the agreement of whoever runs the storage. Everything is written to a new
subdirectory of `--dir` and removed at the end, also when the job is
cancelled or hits its time limit.

## Related work

- **IO500** ([io500.org](https://io500.org)) is the standard HPC storage
  benchmark: IOR and mdtest at scale, "easy" and "hard" patterns, one score,
  and a public list for comparing sites. It needs MPI and long runs and is
  about aggregate performance. storage-roofline is lighter (fio, no MPI,
  minutes) and looks at the single client: how latency and data in flight
  limit it, where its network roof is, and how cold small-file access
  performs. The two complement each other.
- **Empirical I/O roofline.** Z. Zhu, N. Bartelheimer and S. Neuwirth, *An
  Empirical Roofline Model for Extreme-Scale I/O Workload Analysis* (ESSA
  2023), and Z. Zhu and S. Neuwirth, *Characterization of Large-scale HPC
  Workloads with non-naïve I/O Roofline Modeling and Scoring* (ICPADS 2023),
  plot IOPS against I/O intensity (operations per byte) under
  min(peak IOPS, peak bandwidth × intensity), with the peaks from IOR at scale
  and applications placed from Darshan logs. M. Tang, Z. Zhu et al. apply it
  to AWS S3 (SC'25 workshops). Their flat IOPS roof is whatever the full
  system reaches; here the x-axis is data in flight instead, so latency and
  concurrency are visible, which is what explains a single client's speed.
  `roofline_iops.png` draws our points on their axes.
- **Roofline for data services.** P. Carns et al., *Towards Empirical Roofline
  Modeling of Distributed Data Services* (CUG 2025), builds network rooflines
  for RPC services from per-client and per-server peaks, drawn as ranges.

## Development

```bash
pip install -e ".[test]"
pytest            # the end-to-end test runs if fio is installed
```

## Licence

GPL-3.0. See [LICENSE](LICENSE).
