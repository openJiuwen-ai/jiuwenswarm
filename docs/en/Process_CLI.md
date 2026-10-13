# Process CLI: one command, one local task

Install JiuwenSwarm and configure an available default model using its local
configuration. The `jiuwenswarm-process` console script ships with the main
package. A separate SDK and running Gateway, Web, or AgentServer service are
not required.

```shell
jiuwenswarm-process -h
jiuwenswarm-process "Reply only CLI_DEFAULT_OK" --timeout 120
jiuwenswarm-process "Read notes.txt and return its contents" --cwd "D:\cli-test" --show-tools --timeout 120
```

Supplying task text starts one local Runtime, executes the task, performs
cleanup, and exits. Omitting task text starts the existing interactive REPL.
The public input and output are text; request-document execution, the stdio
protocol SDKs, and machine output format options have been removed.

## Public arguments

| Argument | Value | Default / purpose |
| --- | --- | --- |
| `prompt` | Quoted task text | Omit to enter the REPL. |
| `-h`, `--help` | No value | Print help and exit without starting Runtime. |
| `--session` | Existing full Process CLI session ID | Omit to create a new session. |
| `--cwd` | Existing directory | Current process directory; applied before Runtime imports. |
| `--project-dir` | Project directory | Defaults to the command's working directory. |
| `--trusted-dir` | Trusted directory; repeat for multiple directories | Defaults to the project directory if omitted. Does not bypass tool permissions. |
| `--mode` | Runtime mode, such as `agent.code.normal` or `agent.work.normal` | `code.normal` by default. |
| `--work-mode` | `code` or `work` | `code`; an explicit complete mode determines the effective profile. |
| `--timeout` | Positive seconds, including fractions | Omit for no overall execution deadline. Cleanup can take additional time. |
| `--show-reasoning` | No value | Show reasoning only when the model supplies it. |
| `--show-tools` | No value | Show actual tool calls and results. |
| `--debug` | No value | Show detailed diagnostics for the current one-shot task. |

Model connections come from the usual local JiuwenSwarm configuration. Use
the model name and API credentials accepted by the actual service. The local
help output is authoritative for available command arguments.

## Continue a session

```shell
jiuwenswarm-process "Remember ORANGE_LOCAL_5817; reply only REMEMBERED" --mode agent.code.normal --timeout 120
jiuwenswarm-process "What token did I ask you to remember? Reply only the token" --session FULL_SESSION_ID --mode agent.code.normal --timeout 120
```

Use the first turn's **full** session ID; the terminal completion footer may
shorten its display. The existing REPL's `/sessions` control lists full IDs.
Each invocation uses its current arguments. Session continuation does not
implicitly carry a previous invocation's directory or mode flags. Without
`--session`, a new invocation creates a new session.

## Output and interaction

Model answers go to stdout; reasoning, tool details, and failure summaries go
to stderr. For example, a Windows CMD caller can save an answer with:

```cmd
jiuwenswarm-process "Reply only CLI_DEFAULT_OK" --timeout 120 > answer.txt
echo %ERRORLEVEL%
type answer.txt
```

Existing terminal questions and approvals remain available when a real
interactive terminal is attached. If input is unavailable, a required
interaction fails with exit code 4. There is no public host callback protocol.

| Exit code | Meaning |
| --- | --- |
| 0 | Success. |
| 1 | Runtime or model failure. Check the diagnostic log. |
| 2 | Invalid command-line arguments. |
| 4 | Required interaction cannot be answered. |
| 124 | Overall execution timeout. |
| 130 | User interruption. |

REPL catalog queries use a private worker with command-line inputs. Its result
envelope and parent/worker receipts are internal implementation details, not
public noninteractive input/output formats.
