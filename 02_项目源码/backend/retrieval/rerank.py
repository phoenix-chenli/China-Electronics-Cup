"""Optional transparent deterministic rank refinement (not a neural reranker)."""
import re


class ExactContextReranker:
    name = 'exact-year-entity-v1'

    def __call__(self, question, ranking, chunks):
        # Close semantic hits from another year must not displace an exact
        # requested period. Missing years get no invented context.
        years = set(re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', question))
        quoted = re.findall(r'[“「《]([^”」》]{2,40})[”」》]', question)
        def key(pair):
            text = chunks[pair[0]]['text'] + '\n' + chunks[pair[0]].get('retrieval_hint', '')
            return (-sum(year in text for year in years), -sum(term in text for term in quoted), -pair[1], pair[0])
        return sorted(ranking, key=key)
