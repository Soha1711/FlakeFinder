FIX PROMPT: Given the winning root cause and evidence for {test_name}, propose
the smallest diff that resolves it (add teardown fixture / seed RNG / add await
/ revert regressing change). Output a unified diff only.

VERIFICATION PROMPT (separate agent, given ONLY the diff + test name, not the
fix agent's reasoning): Apply this diff to a scratch copy of the repo. Rerun
{test_name} (and its suite neighbors) {N} times. Report the raw pass count.
Do not comment on whether you expect it to work — report only what happened.