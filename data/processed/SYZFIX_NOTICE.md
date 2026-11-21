# Notice for `syzfix_cases.json`

**Source.** Derived from one shard (`train-00000-of-00011.parquet`, 1 of 11) of the SyzFix dataset,
<https://huggingface.co/datasets/xiaoguangwang/syzfix-dataset>, revision `c0c94c4f`, SHA-256
`a98ac4c4dc4572e51c98fee48c507a3b2ff2ed40aa15583c6eed6509e4450af8`. As requested by the dataset card,
please cite the SyzFix repository: <https://github.com/sysec-uic/syzfix>.

SyzFix aggregates public content from syzbot (syzkaller.appspot.com), git.kernel.org, lore.kernel.org
and patchwork.kernel.org. The dataset card states that it is intended for research use and that kernel
patches and mailing-list text remain under their original licences and copyrights.

**What this file contains.** Bug identifiers, syzbot titles, dates, labels derived by this project
(category, subsystem from file paths, address kind), function names from call traces, fix-commit
identifiers and titles, `Fixes:` references, changed-file statistics, links to the original sources,
and short excerpts (≤ 30 lines) of Linux kernel source code changes.

**Licence of the code excerpts.** The `diff_excerpt` fields are excerpts of the Linux kernel source,
which is licensed under the GNU General Public License version 2 (see the kernel's `COPYING` file).
The full changes are available at the linked git.kernel.org commits.

**Not included.** Crash-report text, commit-message prose, reproducers, mailing-list discussions,
patch-revision histories, author names and e-mail addresses. See `docs/SYZFIX.md` for the per-field
licence review.
