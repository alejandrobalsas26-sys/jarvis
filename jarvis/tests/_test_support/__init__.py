"""Test-owned support modules. Importable by spawned worker processes.

NAMED ``_test_support``, NOT ``support``, AND IMPORTED WITHOUT A ``tests.`` PREFIX
=================================================================================
This package used to be ``tests/support`` and was imported as ``tests.support``.
That import resolved from ``jarvis/`` as the working directory and failed from the
REPOSITORY ROOT, which is where the authoritative CI job actually runs
(``python -m pytest -q --tb=short jarvis/tests tests``). The cause is a package
shadow, not a path accident:

    <repo>/tests/__init__.py       exists -> a REGULAR package
    <repo>/jarvis/tests/           no __init__.py -> only a NAMESPACE portion

The import system records a namespace portion and keeps scanning sys.path; a regular
package found at any later entry wins outright. From the repository root both trees
are reachable, so ``tests`` bound to ``<repo>/tests``, which has no ``support``
submodule. The repo-level marker cannot just be deleted — five test basenames collide
across the two trees and that ``__init__.py`` is what keeps them apart.

So the support package no longer travels through the ambiguous ``tests`` name at all.
``_test_support`` is unique across the repository, which makes the import independent
of sys.path ORDER and therefore of the working directory. The leading underscore
follows the convention already used by the sibling helpers in this tree
(``_m62_evaluation_fixtures``, ``_s3q0_synthetic``) and keeps pytest from mistaking
the package for a test module.

It stays UNDER ``jarvis/tests/`` deliberately: ``MANIFEST.in`` prunes ``tests`` and
``scripts/check_package_manifest.py`` rejects any built artifact containing a
``/tests/`` path fragment, so living here is what keeps this crash harness out of the
wheel and the sdist. A sibling directory next to ``core/`` would have escaped both.

tests/test_ci_entrypoint_reality_v69_s5e.py enforces every claim in this docstring.
"""
