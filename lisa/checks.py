from dataclasses import dataclass

from lisa.diff import Chunk

DIFF_FORMAT = (
    "Unified diff of one file. Lines starting with '+' were added, lines starting with '-' were removed, "
    "other lines are unchanged context. '@@' separates distant parts of the file."
)


@dataclass(frozen=True)
class Kind:
    label: str
    criteria: str  # what Jev is told this option means
    why: str  # shown to the PR author: why it matters
    fix: str  # shown to the PR author: what to do


@dataclass(frozen=True)
class Check:
    title: str
    instructions: str
    criteria: dict[str, str]
    kind_instructions: str
    line_instructions: str
    kinds: dict[str, Kind]


# Every check is asked about every chunk of the diff in one request: a yes/no gate,
# which kind of problem it is, and which added line it is on. The kind selects the
# explanation and fix shown in the review comment.
CHECKS = {
    "secret": Check(
        title="Secret",
        instructions="Does this diff add a secret?",
        criteria={
            "true": "An added line contains a real credential such as an API key, access token, password, "
            "private key, or connection string with a password.",
            "false": "No real credentials are added. Placeholders, example values, test fixtures, and references "
            "to environment variables or secret stores are not secrets.",
        },
        kind_instructions="What kind of secret do the added lines in `diff` contain?",
        line_instructions="Which added line in `diff` contains the secret?",
        kinds={
            "api_key": Kind(
                "API key",
                "An API key for a third-party service or internal API.",
                "Anyone who can read this repository, its forks, or its CI logs can use this key to act as your "
                "application and run up usage or reach data it has access to.",
                "Revoke the key with the provider and issue a new one. Load it at runtime from an environment "
                "variable or your secret manager.",
            ),
            "access_token": Kind(
                "Access token",
                "An OAuth, personal access, bot, session, or bearer token.",
                "A leaked access token grants the same access as the account or app it belongs to until it "
                "expires or is revoked.",
                "Revoke the token and issue a new one with the narrowest scopes that work. Inject it at runtime "
                "from your secret store instead of the source.",
            ),
            "password": Kind(
                "Password",
                "A password or passphrase for a user, service account, or database.",
                "Hardcoded passwords end up in every clone and fork and are hard to rotate, because every copy "
                "of the code has to change with them.",
                "Change the password, then read it from an environment variable or secret manager. If this is "
                "only for tests, generate it at runtime or use an obviously fake value.",
            ),
            "private_key": Kind(
                "Private key",
                "A private key such as an RSA, EC, SSH, PGP, or TLS key.",
                "A private key lets anyone impersonate the owner: sign as them, decrypt their traffic, or log in "
                "wherever the matching public key is trusted.",
                "Treat the key as compromised: generate a new key pair and remove trust in the old public key. "
                "Keep private keys out of the repository entirely and mount them at deploy time.",
            ),
            "connection_string": Kind(
                "Connection string with credentials",
                "A database, queue, or service URL with a username and password in it.",
                "The embedded credentials give direct access to the backing service, often bypassing the "
                "application's own access checks.",
                "Rotate the credentials, and build the URL at runtime from configuration that is not committed.",
            ),
            "other": Kind(
                "Credential",
                "Any other kind of secret.",
                "Secrets committed to source control stay in the git history even after they are deleted, and "
                "are visible to everyone with access to the repository.",
                "Rotate the secret, then load it at runtime from an environment variable or secret manager.",
            ),
        },
    ),
    "security": Check(
        title="Security vulnerability",
        instructions="Does this diff introduce a security vulnerability?",
        criteria={
            "true": "The change adds or enables something exploitable, such as injection, cross-site scripting, "
            "weakened authentication or authorization, unsafe eval or deserialization, path traversal, "
            "server-side request forgery, weak cryptography, disabled TLS or CSRF checks, or logging of "
            "sensitive data.",
            "false": "The change does not make the code less secure.",
        },
        kind_instructions="What kind of security vulnerability does `diff` introduce?",
        line_instructions="Which added line in `diff` introduces the security vulnerability?",
        kinds={
            "injection": Kind(
                "Injection",
                "A SQL query, shell command, or other interpreter input built by concatenating or interpolating "
                "variables.",
                "If any part of the interpolated value can be influenced by a user, they can change the meaning "
                "of the query or command and read, modify, or delete data, or run commands on the host.",
                "Use parameterized queries or prepared statements, and pass command arguments as a list instead "
                "of a shell string. Never build interpreter input with string formatting.",
            ),
            "xss": Kind(
                "Cross-site scripting",
                "Untrusted data inserted into HTML or the DOM without escaping, for example via innerHTML, "
                "dangerouslySetInnerHTML, v-html, or marking a string as safe.",
                "An attacker who controls the inserted value can run JavaScript in other users' browsers, "
                "stealing sessions or acting on their behalf.",
                "Render the value as text (textContent, or the framework's default escaping) or sanitize it with "
                "a vetted HTML sanitizer before inserting it.",
            ),
            "access_control": Kind(
                "Weakened access control",
                "An authentication, authorization, or permission check is removed, bypassed, or weakened.",
                "Users may be able to reach data or actions they should not have access to.",
                "Restore the check, or move it to a layer every code path goes through. Add a test that an "
                "unauthorized request is rejected.",
            ),
            "code_execution": Kind(
                "Unsafe code execution or deserialization",
                "Data that may come from users is evaluated or deserialized, for example with eval, exec, "
                "new Function, pickle.loads, yaml.load, or unserialize.",
                "Evaluating or deserializing attacker-controlled data usually leads to remote code execution.",
                "Parse data with a data-only format and loader (JSON, yaml.safe_load) and validate the result. "
                "Never evaluate user input as code.",
            ),
            "path_traversal": Kind(
                "Path traversal",
                "A file system path built from input that may come from users, without validation.",
                "Input like `../../etc/passwd` lets an attacker read or overwrite files outside the intended "
                "directory.",
                "Resolve the final path and check that it stays inside the allowed base directory, or map user "
                "input to an allowlist of known files.",
            ),
            "ssrf": Kind(
                "Server-side request forgery",
                "A network request to a URL or host that may come from users, with no allowlist.",
                "Attackers can make your server call internal services or cloud metadata endpoints that are not "
                "reachable from outside.",
                "Only allow requests to an explicit allowlist of hosts, and block private and link-local "
                "addresses after DNS resolution.",
            ),
            "weak_crypto": Kind(
                "Weak cryptography",
                "Weak or broken cryptography, such as MD5 or SHA-1 for passwords, ECB mode, a hardcoded IV or "
                "salt, or a non-cryptographic random generator for tokens or keys.",
                "Weak algorithms and predictable randomness let attackers crack hashes, decrypt data, or guess "
                "tokens.",
                "Hash passwords with bcrypt, scrypt, or Argon2; use an authenticated cipher such as AES-GCM with "
                "random nonces; generate tokens with a cryptographically secure random source.",
            ),
            "disabled_verification": Kind(
                "Disabled security verification",
                "TLS certificate verification, signature verification, or CSRF protection is turned off.",
                "Without verification, attackers on the network can intercept or forge traffic, or trick users "
                "into submitting requests they did not intend.",
                "Keep verification on. For internal certificates, trust the specific CA instead of disabling "
                "checks.",
            ),
            "insecure_config": Kind(
                "Permissive security configuration",
                "Security configuration made more permissive, such as CORS allowing any origin with credentials, "
                "debug mode in production, world-writable permissions, or public storage.",
                "Permissive defaults expose data and internals to anyone who finds them.",
                "Scope the setting to what is needed: explicit origins, debug off in production, least-privilege "
                "permissions, private storage.",
            ),
            "sensitive_logging": Kind(
                "Sensitive data exposure",
                "Passwords, tokens, secrets, or personal data are logged, printed, or returned in a response.",
                "Logs and responses are widely readable and long-lived, so sensitive values in them leak to far "
                "more people and systems than intended.",
                "Remove the sensitive fields or redact them before logging or returning the data.",
            ),
            "other": Kind(
                "Security vulnerability",
                "Any other security vulnerability.",
                "This change appears to make the code less secure.",
                "Review how untrusted input reaches this line and what an attacker could do with it.",
            ),
        },
    ),
    "complexity": Check(
        title="Unneeded complexity",
        instructions="Does this diff add unneeded complexity?",
        criteria={
            "true": "The added code is noticeably more complex than the change requires, for example "
            "single-use abstractions, needless layers or configuration, dead or commented-out code, "
            "or convoluted control flow where a simpler version would do the same job.",
            "false": "The added code is about as simple as the change allows.",
        },
        kind_instructions="What kind of unneeded complexity do the added lines in `diff` introduce?",
        line_instructions="Which added line in `diff` is where the unneeded complexity starts?",
        kinds={
            "over_abstraction": Kind(
                "Unnecessary abstraction",
                "An interface, base class, factory, wrapper, or generic parameter with a single use and no "
                "clear need.",
                "Abstractions with one implementation add indirection readers have to follow without making "
                "the code more flexible in any way that is used today.",
                "Inline it and use the concrete code directly. Introduce the abstraction when a second real use "
                "appears.",
            ),
            "speculative_options": Kind(
                "Speculative configuration",
                "Options, flags, parameters, or extension points that nothing uses yet.",
                "Every option is a code path to test and maintain, and unused ones mostly add ways to break "
                "things.",
                "Remove the options nothing needs yet and hardcode the value that is actually used.",
            ),
            "dead_code": Kind(
                "Dead or commented-out code",
                "Code that is never called, commented-out code, or branches that can never run.",
                "Dead code misleads readers about what the program does and still has to be kept compiling and "
                "reviewed.",
                "Delete it. Version control keeps the history if it is ever needed again.",
            ),
            "convoluted_flow": Kind(
                "Convoluted control flow",
                "Deeply nested conditionals or loops, or roundabout logic, where a straightforward version would "
                "do the same thing.",
                "Nested and roundabout logic is harder to read, test, and change safely.",
                "Flatten it with early returns and guard clauses, or split the logic into small, well-named "
                "functions.",
            ),
            "duplication": Kind(
                "Duplicated logic",
                "The same logic repeated in several places in the diff.",
                "Duplicated logic drifts apart over time, so a fix in one copy is easily missed in the others.",
                "Keep one implementation and call it from each place.",
            ),
            "other": Kind(
                "Unneeded complexity",
                "Any other unneeded complexity.",
                "This change looks more complex than what it accomplishes.",
                "Look for a simpler way to get the same behavior with less code.",
            ),
        },
    ),
}


@dataclass
class Finding:
    check: str
    kind: str
    file: str
    line: int
    probability: float
    text: str = ""  # the flagged line, used to recognize the same finding across pushes

    @property
    def title(self) -> str:
        return CHECKS[self.check].title

    @property
    def details(self) -> Kind:
        return CHECKS[self.check].kinds[self.kind]


def build_request(chunk: Chunk, model: str) -> dict:
    questions = {}
    line_options = {str(line.number): line.text.strip()[:200] or "(blank line)" for line in chunk.added}
    for key, check in CHECKS.items():
        questions[key] = {"type": "noul", "instructions": check.instructions, "criteria": check.criteria}
        questions[f"{key}_kind"] = {
            "type": "choice",
            "instructions": check.kind_instructions,
            "criteria": {name: kind.criteria for name, kind in check.kinds.items()},
        }
        if len(line_options) > 1:
            questions[f"{key}_line"] = {
                "type": "choice",
                "instructions": check.line_instructions,
                "criteria": line_options,
            }
    return {
        "model": model,
        "state": {"file": chunk.file, "diff_format": DIFF_FORMAT, "diff": chunk.diff},
        "questions": questions,
    }


def interpret(chunk: Chunk, answers: dict, threshold: float) -> list[Finding]:
    """Every check answered yes (probability at or above threshold) is a finding."""
    findings = []
    for key, check in CHECKS.items():
        probability = answers.get(key, {}).get("noul", 0)
        if probability < threshold:
            continue
        kind = answers.get(f"{key}_kind", {}).get("choice")
        line = answers.get(f"{key}_line", {}).get("choice")
        number = int(line) if line else chunk.start_line
        findings.append(
            Finding(
                check=key,
                kind=kind if kind in check.kinds else "other",
                file=chunk.file,
                line=number,
                probability=probability,
                text=next((l.text for l in chunk.added if l.number == number), ""),
            )
        )
    return findings
