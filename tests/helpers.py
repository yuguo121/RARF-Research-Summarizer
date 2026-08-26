from __future__ import annotations

import json

QUOTE = (
    "CEO duality is far too complex to be considered dichotomously, with dual CEOs "
    "viewed as wielding unchecked power and separated CEO and chair roles embodying "
    "the essence of good governance."
)

PAGE_TEXT = (
    "Abstract\n"
    "We review the CEO duality literature. Duality is a double-edged sword.\n\n"
    "Introduction\n"
    "What are the implications of CEO duality? " + QUOTE + "\n\n"
    "Theory\n"
    "Agency theory suggests boards should be independent from management. "
    "Stewardship theory argues duality promotes unity of leadership.\n\n"
    "Methods\n"
    "This review includes 48 publications from management and finance journals.\n\n"
    "Results\n"
    "Dalton et al. (1998) found no empirical link between CEO duality and firm performance.\n\n"
    "Discussion\n"
    "Future research should treat duality as more than a dichotomy.\n\n"
    "Conclusion\n"
    "We urge scholars to explore new theories, methods, and contexts.\n"
)


def envelope(value, status="present", confidence=0.9, page=2, quote=None):
    return {
        "status": status,
        "confidence": confidence,
        "value": value,
        "evidence": [{"page": page, "quote": quote or QUOTE[:80]}] if status == "present" else [],
        "warnings": [],
    }


def theory_payload():
    return {
        "citation": envelope("Krause, R., Semadeni, M., & Cannella, A. A., Jr. (2014). CEO duality: A review and research agenda. Journal of Management, 40(1), 256-286."),
        "research_type": envelope("theoretical"),
        "research_question": envelope("What are the implications of CEO duality, and how should future research study it?"),
        "why_important": envelope("Boards' use of duality has changed and theory remains unresolved despite mixed evidence."),
        "framing": envelope(
            {
                "primary_basis": "theory-led",
                "secondary_style": "theoretical",
                "rationale": "The paper organizes a literature review around competing theories of duality rather than estimating one causal effect.",
            }
        ),
        "key_argument": envelope(
            [
                {
                    "quote": QUOTE,
                    "page": 2,
                    "academic_paraphrase": "Treating duality as a binary indicator of good versus bad governance oversimplifies a more nuanced phenomenon.",
                    "plain_language": "Whether the CEO is also chair is more complicated than a simple yes/no governance score.",
                    "causal_formulation": "If duality is modeled only as a dichotomy, tests of its performance effects will remain inconclusive.",
                }
            ]
        ),
        "theoretical_lenses": envelope("agency theory; stewardship theory; resource dependence theory"),
        "hypotheses": envelope(None, status="not_applicable"),
        "key_variables": envelope(
            [
                {
                    "class": "IV",
                    "name": "CEO duality",
                    "nominal_definition": "A single individual serving as both CEO and board chair",
                },
                {
                    "class": "DV",
                    "name": "firm performance",
                    "nominal_definition": "Organizational effectiveness or shareholder returns as studied in the reviewed literature",
                },
            ]
        ),
        "contribution": envelope("Synthesizes duality research and proposes theoretical, methodological, and contextual agenda."),
        "limitations": envelope("Narrative review without a new meta-analysis."),
        "so_what": envelope("Boards should not treat splitting the chair role as an automatic governance fix."),
        "most_interesting": envelope("The call to abandon a purely dichotomous view of duality."),
    }


def method_payload():
    na = lambda: envelope(None, status="not_applicable")
    return {
        "context": envelope("Public corporations, especially large U.S. firms discussed in the reviewed studies"),
        "unit_of_analysis": na(),
        "sampling_strategy": envelope("archival literature search of top management and finance journals"),
        "sample": envelope("48 publications that proposed or tested hypotheses about CEO duality"),
        "sample_type": envelope(None, status="not_applicable"),
        "data_sources": envelope("Published articles identified by keyword search"),
        "measures": envelope(None, status="not_applicable"),
        "statistical_models": na(),
        "endogeneity": na(),
        "findings": envelope("No conclusive duality-performance link; antecedents remain understudied."),
    }


def reconcile_payload():
    payload = theory_payload()
    payload.update(method_payload())
    return payload


def json_response(payload: dict) -> str:
    return "```json\n" + json.dumps(payload) + "\n```"
