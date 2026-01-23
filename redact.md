update catch_dotenv to redact_dotenv

Update logic
It's purpose is now to prevent .env files from being committed AND simultaneously preserve the format of the .env contents (redacting the secret values), producing an example .env. It should:

Redaction Requirements:
0. All search target string are case-insensitive
1. REDACT VALUE if 
    * KEY contains:
        - 'PASSWORD'
        - 'API'
        - 'KEY'
        - 'TOKEN'
        - 'SECRET'
    * Inline Comment contains 'REDACT'
2. DO NOT REDACT if:
    - Line contains comment containing 'DNR'  
    - VALUE contains reference to expandable variable

REDACTION FORMATTING
2. If KEY contains case-insensitive 'PASSWORD', redact all characters in VALUE
3. If KEY contains case-insensitive 'API' or 'KEY' or 'TOKEN' or 'SECRET', redact chars according to offset. 
    If COMMENT contains python array range specifier, use that as OFFSET
        Default is [3:] - all characters except first 3
        [:3] - all characters except last 3
        [10:] - all characters except first 10
        [:] or [*] - all characters 
4. If VALUE contains url, preserve schema
5. Comments, Spaces, NewLines, and any other formatting MUST be preserved


Pre-Commit Behavior

if example.env exists:
    if example.env requires redaction according to current ruleset:
        apply redaction to non-redacted values
    elif example.env requires no redaction according to current ruleset:
        PASS
elif example.env does not exist:
    create example.env in same dir as .env (for each .env found)
    apply redaction to example.env

if .env is staged:
    FAIL