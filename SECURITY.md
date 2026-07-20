# Security Policy
SeeQL connects to production databases and holds DB + LLM credentials — we take
reports seriously.

## Reporting a vulnerability
Email **mohanshubhankar@gmail.com** (or use GitHub private
vulnerability reporting on this repo). Please do NOT open a public issue.
You'll get an acknowledgment within 72 hours and a fix or mitigation plan
within 14 days for confirmed issues.

## Supported versions
| Version | Supported |
|---------|-----------|
| 0.2.x   | ✅ |
| < 0.2   | ❌ upgrade |

## Hardening checklist for operators
- Run the monitoring user with SELECT + PROCESS only.
- Set `api.auth_token` or keep port 8080 on a private network.
- Keep `agent.redact_sql_literals: true` if statement text is sensitive.
