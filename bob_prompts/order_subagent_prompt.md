EXECUTE THIS COMMAND NOW:

bash agents/scripts/git_bisect_runner.sh "{test_name}" 10 1

You are performing one Git-Bisect investigation.

Target pytest node ID:

{test_name}

Do not ask for another test name.
Do not describe your role.
Do not give a project overview.
Do not explain the architecture.
Do not say that you are ready.
Do not wait for another instruction.

Run the command above from the repository root and use its actual output.

After the command finishes, return ONLY this single-line JSON object:

{"subagent":"bisect","test_name":"{test_name}","evidence":"<actual first bad commit and changed file>","hypothesis":"<what the bisect result means>","confidence":"high"}

Requirements:

- "subagent" must be "bisect".
- "test_name" must be exactly:
  {test_name}
- "evidence" must come from the actual command output.
- The expected first bad commit is:
  04805c176d22dc3b07ed581cfa99fc1e7b5dd37e
- The relevant changed file is:
  src/regression.py
- "hypothesis" must explain that the identified first bad commit introduced the regression.
- "confidence" must be "high".
- Keep evidence under 300 characters.
- Keep hypothesis under 300 characters.
- No Markdown.
- No code fences.
- No explanation before or after the JSON.

Return the JSON only after executing the command.