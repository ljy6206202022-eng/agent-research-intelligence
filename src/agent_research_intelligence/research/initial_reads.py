"""Serialize exclusive-lease catalog reads; overlap only independent status."""
from concurrent.futures import ThreadPoolExecutor


def run(workspace, question_id, *, catalog=None, account_status=None):
    if catalog is None:
        from agent_research_intelligence.research.catalog import Catalog
        catalog = Catalog(workspace)
    if account_status is None:
        from agent_research_intelligence.connectors.youtube_account import YouTubeAccount
        account_status = lambda: YouTubeAccount(workspace).status()
    with ThreadPoolExecutor(max_workers=1) as pool:
        independent = pool.submit(account_status)
        question = catalog.read(question_id)
        terms = question['payload']['search_terms']
        evidence = catalog.search('evidence', terms)
        dossiers = catalog.search('dossier', terms)
        sources = catalog.search('source', terms)
        status = independent.result()
    return {'question': question, 'evidence': evidence, 'dossiers': dossiers,
            'sources': sources, 'youtube_account': status,
            'catalog_schedule': 'SERIAL_EXCLUSIVE_STORE_READS',
            'account_schedule': 'INDEPENDENT_PARALLEL_READ'}
