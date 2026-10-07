# Public toolkit instructions

This repository contains generic documentation and software only.

- Keep scientific sources, manuscripts, ideas, structures, input/output files, results, and real workstation configurations in a separate private workspace.
- Read only the files needed for the current change. Do not search users' private research directories to obtain examples.
- Use synthetic fixtures for tests. Label them clearly; never turn private production cases into public examples by changing names alone.
- Require explicit specifications for physical values, source geometry, species order, constraints, environment identity, and fresh/restart contracts.
- Local preparation, remote submission, execution completion, convergence, and scientific acceptance are separate states.
- Never include licensed potential datasets or copy commercial software into this repository.
- Make related changes in functional batches. Run affected checks; reuse successful checks for unchanged content. Use hashes for package identity and concrete integrity concerns.
- Remote operations, notifications, and publication require the user's authorization for the named action.
- Before publishing, review the actual files, Git index, all outgoing history, and release manifest. Run `scripts/check_public_release.py` in this independent repository. A clean scan does not replace human confidentiality review.
- Preserve existing files and other contributors' changes. Do not rewrite or import private repository history.
