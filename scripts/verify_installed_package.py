"""Run with ``python -I scripts/verify_installed_package.py`` after pip install.

Isolated mode intentionally prevents the source checkout from satisfying missing
imports in the wheel. This smoke test also exercises installed protocol data.
"""
import importlib
import json
import sys
import tempfile
from pathlib import Path


def main():
    if not sys.flags.isolated:
        raise RuntimeError('Run with python -I so source imports cannot hide packaging defects')
    checkout = Path(__file__).resolve().parents[1]
    for name in (
        'critic_runner', 'argument_contracts', 'argument_gate_common', 'project_lock',
        'cli.run', 'cli.campaign', 'contracts.gate_a', 'contracts.gate_b',
        'document_review_components', 'document_review_stores.ingestion',
        'document_review_stores.audits', 'document_review_stores.revision',
        'document_review_stores.exports', 'document_review_studio',
    ):
        module = importlib.import_module(name)
        if Path(module.__file__).resolve().is_relative_to(checkout):
            raise RuntimeError(f'{name} was imported from the checkout instead of the wheel')

    import critic_runner
    from cli.core import IR_SOCIAL_SCIENCE_RULES, load_protocol
    from argument_ir import validate_check_library
    from document_review_studio import DocumentReviewProject
    from document_review_model import ReviewContext

    for protocol in ('critic-individualist', 'critic-contrastivist', 'critic-social-science',
                     'critic-natural-science', 'critic-engineering', 'citation-auditor'):
        text, raw = load_protocol(protocol)
        assert text and raw, protocol
    assert not validate_check_library(json.loads(IR_SOCIAL_SCIENCE_RULES.read_text(encoding='utf-8')))
    assert critic_runner.main(['list']) == 0
    with tempfile.TemporaryDirectory() as directory:
        project = DocumentReviewProject.create(directory, filename='draft.md', content=b'# Draft\n\nA short example.')
        project.confirm_extraction('confirm')
        context = ReviewContext(document_type='professional document', jurisdiction='unknown',
                                effective_date='unknown', publisher_type='internal', audience='editors')
        project.confirm_context(context.to_dict())
        project.run_local_prechecks(['expression_ambiguity'])
        assert project.view()['project']
        assert not project.integrity_errors()
    print('Installed package: imports, protocol data, rules, CLI and Studio workflow verified')


if __name__ == '__main__':
    main()
