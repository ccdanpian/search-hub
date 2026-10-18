# authority.json: how it works and how to maintain it

`searchhub/core.py` reads `authority.json` on every search. It decides which URLs are worth reading first. It never removes a result because of its tier; it only changes ranking, except for `skip_hosts` and `skip_ext`, which do remove results.

## Fields

| Field | Meaning |
|---|---|
| `tier3` | Primary or official sources: vendor and project docs, standards bodies, regulators, peer-reviewed journals, wire services. Score bonus **+0.30**. |
| `tier2` | Reputable but secondary: mainstream media, technical communities, well-known blogs, consumer health sites, preprints. Score bonus **+0.15**. |
| `skip_hosts` | Hosts dropped from the selection (video and social sites that cannot be read as text). |
| `skip_ext` | File extensions dropped from the selection (images, video, archives). `.pdf` was removed in 1.1 because `web_fetch` now reads PDFs. |
| `_doc` | A comment; ignored by the code. |

Any domain not listed is tier 1 (no bonus).

## Matching rules

- A domain matches an entry if it equals the entry or ends with `.` plus the entry. `en.wikipedia.org` matches `wikipedia.org`; `evilgithub.com` does not match `github.com`.
- Bare suffixes work too: `edu`, `gov`, `edu.cn`, `ac.uk` put every such domain in tier 2.
- tier3 is checked first, so a domain in both tiers counts as tier 3. Keep a domain in only one tier.
- Matching is by host only, not by path. You cannot rate `example.com/docs` differently from `example.com`.

## Categories currently covered

| Area | Tier 3 | Tier 2 |
|---|---|---|
| General reference and news | wikipedia, Reuters, AP, BBC, Xinhua, People's Daily, gov.cn | NYT, Guardian, CNN, Caixin, Al Jazeera, DW, NPR, PBS, Nikkei, SCMP, CNBC, Forbes, and similar |
| Software and standards | official language, OS, cloud and AI vendor docs; W3C, IETF, ISO, IEEE, ACM | Stack Exchange, Hacker News, LWN, DigitalOcean, Real Python, MDN-style blogs, npm, PyPI, Medium, dev.to |
| Science and policy | Nature, Science, Springer, ScienceDirect, arXiv, OpenReview, ACL Anthology, WHO, UN, World Bank, IMF, OECD, EU, gov.uk | edu, gov, ac.uk, science news sites |
| Finance | Bloomberg, FT, WSJ, Economist, SEC | Investopedia, Forbes |
| **Medicine** | see below | see below |

### Medicine

Tier 3 (regulators, guideline bodies, evidence databases, major journals and societies):
- Regulators and public health: `fda.gov`, `ema.europa.eu`, `cdc.gov`, `ecdc.europa.eu`, `who.int`, `nih.gov`, `cancer.gov`, `medlineplus.gov`, `clinicaltrials.gov`, `nhs.uk`, `chinacdc.cn`
- Guidelines and evidence synthesis: `nice.org.uk`, `cochranelibrary.com`, `cochrane.org`
- Literature databases: `pubmed.ncbi.nlm.nih.gov`, `ncbi.nlm.nih.gov` (includes PubMed Central), `europepmc.org`
- Journals: NEJM, The Lancet, BMJ, JAMA Network, Annals/ACP, AHA journals, Diabetes journals, ASCO, JACC, Cell, Nature, PNAS, PLOS
- Societies: `escardio.org`, `heart.org`, `cancer.org`, `cma.org.cn`

Tier 2 (useful, but reviewed less strictly or aimed at the general public):
- `mayoclinic.org`, `clevelandclinic.org`, `hopkinsmedicine.org`, `healthline.com`, `webmd.com`, `medicalnewstoday.com`, `merckmanuals.com`, `medscape.com`, `uptodate.com`, `drugs.com`, `kff.org`, `dxy.cn`
- `medrxiv.org` is tier 2 on purpose: preprints are not peer reviewed.

Judgement calls you may want to change:
- `chinacdc.cn` and `cma.org.cn` were added from memory and not checked against a live page. Open them once to confirm they are the intended organizations.
- `uptodate.com` is mostly behind a paywall, so pages often cannot be read; consider moving it to tier 1 or removing it.
- `dxy.cn` (DXY) is a professional community with mixed-quality user posts; tier 2 is generous.
- `healthline.com`, `webmd.com` and `medicalnewstoday.com` are commercial consumer sites. They rank above unknown sites but below sources in tier 3.

## How to edit

1. Add the bare domain (no `https://`, no `www.`, no path) to one tier. Use the registrable domain unless you want to rate only one subdomain, for example `docs.python.org` instead of `python.org`.
2. Keep the file valid JSON (commas between entries, none after the last one).
3. Check it:
   ```bash
   python -c "import json; c=json.load(open('authority.json', encoding='utf-8')); print(len(c['tier3']), len(c['tier2']), set(c['tier3']) & set(c['tier2']))"
   ```
   The last value should print `set()`.
4. Try a real query: `python -m searchhub search "your topic" --text` and look at the `auth=` value of each result.

## Limits

- The lists were written from general knowledge and have not been audited domain by domain.
- Tier 3 means "primary or official source", not "always correct". A tier 3 page can still be outdated; `--recent` helps for time-sensitive topics.
- Chinese-language coverage for medicine is thin (only `chinacdc.cn`, `cma.org.cn`, `dxy.cn`, `gov.cn`). Add hospital, society and regulator sites you actually use.

## Scoring formula (searchhub/core.py, `rank`)

`score = (rrf/top + 0.15*(authority-1) + recency + prior) * (0.4 + 0.6*min(1, relevance/0.6))`, then multiplied by `(0.3 + 0.7*p)` when the LLM judge is on.

- `rrf`: sum over engines of 1/(60+rank), divided by the best rrf in this result set.
- `authority`: 1, 2 or 3 from this file. Bonus is +0.15 for tier 2 and +0.30 for tier 3.
- `recency`: up to 0.10 (0.35 with `--recent`) for pages less than a year old; none without a date. The `--recent` weight was lowered from 0.6 to 0.35 after a test where news reposts from a few days ago outranked an authority-3 guideline page. It is now close to the tier-3 bonus (0.30), so a fresh repost no longer beats an official source by default.
- `prior`: `0.03 * rerank_score/3` for results that came from the DeepSeek backend (its own 0-3 relevance score), else 0. It only breaks near-ties: 0.1 was tried and shifted selections towards DeepSeek without improving graded quality.
- `relevance`: 0..1 query-term overlap. Search operators (`site:`, `-site:`, `after:`, `before:`, `lang:`), quotes and `-excluded` words are not counted as terms. Below 0.05 with a single engine means the result is dropped.

Before scoring, results outside a `site:` in the query (or inside a `-site:`) are dropped, unless none match, and the 2-per-domain cap is lifted for `site:` queries. Hosts are compared without `www.` and default ports (`:443`, `:80`).

The constants are hand-picked. They were adjusted on about 10 test queries in 1.0 and checked against the 32-query set in `evals/` in 1.1 (`docs/EVALUATION.md`), not on a large benchmark.

## Known weak spots in the lists

- Matching is by whole domain, so one entry rates every page and subdomain on it. `aliyun.com` used to rate software mirror pages (`mirrors.aliyun.com`) as tier 2; it was replaced by `help.aliyun.com` and `developer.aliyun.com`. Prefer specific subdomains for large sites that host mixed content.
- `medium.com` and `substack.com` match every author subdomain, good or bad.
- Chinese news portals are only partly covered (added: thepaper.cn, cnr.cn, chinanews.com.cn, cctv.com, yicai.com, ithome.com and a few more). Aggregators such as toutiao.com, 163.com and news.qq.com stay at tier 1 on purpose, because they mostly host reposts.
