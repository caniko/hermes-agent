# Supervised kernel lifecycle

Supervised kernel setup stages its runner, tool stubs, and private environment
in one admitted job, followed by the original fenced interpreter launch. File
contents still travel only on stdin, with owner-only directories and files.
This reduces serialized authority/control round trips within the existing
ownership deadlines; it does not bypass job admission or settlement.

Process-output drains consume one fresh state/exit/output frame at their current
offset. A short read of the regular target log proves its current EOF; a full
64-KiB frame requires another observation before completion. Streaming readers
do not share the legacy split-query cache lock. Unknown target state still holds
ownership, and descendant-wait handles require a settled cgroup.
