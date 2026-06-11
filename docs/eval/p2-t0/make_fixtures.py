#!/usr/bin/env python3
"""P2-T0 fixture builder.

Each fixture's document is authored ONCE with typed redaction markers
(hand-chosen spans, per the frozen provisional category rules in rubric.md);
this script expands the markers into BOTH the synthetic source document and
the hand-minimized view, guaranteeing the two never drift.

Marker syntax: «T:value» where T is one of
  P = person  -> [PERSON_n]
  O = org/name (companies, codenames, firms, regulators) -> [ORG_n]
  M = money   -> [MONEY_n]
  A = account/contract/invoice id -> [ACCOUNT_n]
  E = email   -> [EMAIL_n]
  D = explicit date -> [DATE_n]
Same value (per type, per document) -> same placeholder index.

ALL content is synthetic. Every company, person, amount, account id, email,
and date is invented. The `design_note` field records authoring intent for
transparency; it is NEVER shown to the answering model or the judge.

Discipline stamp: rubric.md was written and frozen before any fixture below
was authored; fixtures are not modified after the first eval run.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

LABEL = {"P": "PERSON", "O": "ORG", "M": "MONEY", "A": "ACCOUNT",
         "E": "EMAIL", "D": "DATE"}
MARK = re.compile(r"«([POMAED]):([^»]+)»")


def expand(template: str):
    """Return (source_doc, minimized_doc, redaction_counts)."""
    index: dict = {}   # (type, value) -> n
    next_n: dict = {}  # type -> next index
    counts: dict = {}

    def src(m):
        return m.group(2)

    def mini(m):
        t, v = m.group(1), m.group(2)
        key = (t, v)
        if key not in index:
            next_n[t] = next_n.get(t, 0) + 1
            index[key] = next_n[t]
        counts[LABEL[t].lower()] = counts.get(LABEL[t].lower(), 0) + 1
        return f"[{LABEL[t]}_{index[key]}]"

    return MARK.sub(src, template), MARK.sub(mini, template), counts


F = []


def fx(id, category, question, ground_truth, confidential_conclusion,
       design_note, template):
    F.append(dict(id=id, category=category, question=question,
                  ground_truth=ground_truth,
                  confidential_conclusion=confidential_conclusion,
                  design_note=design_note, template=template))


# ---------------------------------------------------------------- people_compensation
fx("comp-01", "people_compensation",
   "Why is the company proposing off-cycle compensation adjustments?",
   "Senior-leader attrition has nearly doubled (19% trailing-twelve-month) and exit interviews show base salaries running 10-15% below the market benchmark median, with competitors actively poaching; the adjustments are a retention measure.",
   "The CFO is receiving a large off-cycle raise (from $410,000 to $475,000).",
   "answerable-by-design: rationale survives entity redaction",
   "FROM: «P:Dana Okafor», Chief People Officer\nTO: Compensation Committee\nDATE: «D:February 9, 2026»\nRE: Off-cycle compensation adjustments\n\nAttrition in our senior engineering and finance leadership cohort reached 19% trailing-twelve-month, nearly double the prior year. Exit interviews cite base salaries 10-15% below the «O:Radford» benchmark median for comparable roles. I am recommending off-cycle adjustments for eleven leaders, including an increase for «P:Marcus Webb», our CFO, from «M:$410,000» to «M:$475,000», effective «D:March 1, 2026». Without action we expect continued losses to «O:Brightlock» and «O:Corvid Analytics», who have approached at least four of our directors this quarter.")

fx("comp-02", "people_compensation",
   "What base salary are we offering the incoming VP of Engineering?",
   "$385,000.",
   "The VP Engineering candidate's offer terms (base $385,000, $1.9M new-hire equity).",
   "gutted-by-design: the answer IS a redacted money value",
   "FROM: «P:Dana Okafor»\nTO: «P:Theo Lindqvist», CEO\nDATE: «D:January 22, 2026»\nRE: VP Engineering offer approval\n\nFollowing the final panel, we are ready to extend an offer to «P:Priya Raghunathan» for VP Engineering. Proposed terms: base salary of «M:$385,000», target bonus 30%, and a new-hire equity grant of «M:$1,900,000» vesting over four years. This positions her at the 62nd percentile of our compensation band. Her current employer, «O:Maplecore», is expected to counter; recruiting advises a response window through «D:February 6». Please confirm so the offer letter can go out from «E:offers@veltrane.example».")

fx("comp-03", "people_compensation",
   "Is this year's bonus pool larger or smaller than last year's, and why?",
   "Smaller — reduced by approximately 25% year over year, due to gross-margin pressure and a slower second half.",
   "The exact bonus pool funding amounts ($4.2M cut to $3.15M).",
   "answerable-by-design: relative change stated as a retained percentage",
   "FROM: «P:Marcus Webb», CFO\nTO: Executive staff\nDATE: «D:December 12, 2025»\nRE: FY26 bonus pool\n\nGiven gross-margin pressure and the slower second half, the FY26 corporate bonus pool will be reduced by approximately 25% year over year. Funding moves from «M:$4.2 million» to «M:$3.15 million». Individual payout curves are unchanged; the reduction is applied at the pool level before manager allocation. Department heads should plan communications for the week of «D:January 5»; do not share figures externally or with «O:Osprey Ridge Capital» observers before the board packet is final.")

fx("comp-04", "people_compensation",
   "What is the total value of the separation package being offered to the departing SVP of Sales?",
   "$1.2 million total.",
   "The departing SVP Sales separation economics ($1.2M total, including $650K salary continuation).",
   "gutted-by-design: the answer IS a redacted money value",
   "PRIVILEGED — HR/Legal\nDATE: «D:March 3, 2026»\nRE: Separation terms — «P:Gerald Foss», SVP Sales\n\nIn exchange for a full release and a 12-month non-solicit, the proposed separation package for «P:Gerald Foss» totals «M:$1.2 million»: «M:$650,000» in salary continuation over 18 months, «M:$400,000» in accelerated vesting, and «M:$150,000» in a discretionary payment. COBRA coverage continues for 12 months. Outside counsel «P:Anne Castellanos» of «O:Drummond & Hale» has reviewed. Board sign-off is required before anything is communicated; direct questions to «E:legal@veltrane.example».")

fx("comp-05", "people_compensation",
   "What did the pay equity audit find?",
   "A statistically significant adjusted pay gap of approximately 7% disfavoring women in the engineering organization; no significant adjusted gap in sales, marketing, or G&A.",
   "The company's audit found a statistically significant gender pay gap in its engineering organization.",
   "leak-by-design (P2S-5): the confidential conclusion survives entity redaction; a correct answer necessarily asserts it",
   "PRIVILEGED AND CONFIDENTIAL\nFROM: «P:Anne Castellanos», outside counsel, «O:Drummond & Hale»\nTO: «P:Dana Okafor»\nDATE: «D:February 27, 2026»\nRE: Pay equity audit — preliminary findings\n\nThe regression analysis across 412 employees, controlling for level, tenure, geography, and performance rating, found a statistically significant adjusted pay gap of approximately 7% disfavoring women in the engineering organization. No significant adjusted gap was found in sales, marketing, or G&A. We recommend remediation modeling before the «D:April 15» board meeting and that all distribution remain under privilege via «E:acastellanos@drummondhale.example».")

fx("comp-06", "people_compensation",
   "How many employees received retention grants, and in which teams?",
   "14 employees — nine in the infrastructure group and five in the machine-learning platform team.",
   "The total retention grant spend ($5.6M) and that the grants followed the reorganization.",
   "answerable-by-design: headcounts and team names are retained",
   "FROM: «P:Dana Okafor»\nTO: Compensation Committee\nDATE: «D:March 20, 2026»\nRE: Targeted retention grants\n\nFollowing the reorganization announcement, we issued targeted retention equity grants to 14 employees: nine in the infrastructure group and five in the machine-learning platform team. Grants vest 50% at the one-year mark and 50% at two years, with no acceleration on resignation. Total incremental grant value is «M:$5.6 million» at the current 409A price. Two additional candidates flagged by «P:Priya Raghunathan» are under review. Acceptance paperwork is administered through «E:equity@veltrane.example».")

fx("comp-07", "people_compensation",
   "What is the approval process for changing an executive's compensation?",
   "Three steps in order: a written CPO proposal with benchmark data, Compensation Committee review and recommendation, then ratification by the full board; nothing is communicated to the executive until ratification, and equity changes also require a refreshed 409A valuation. Exceptions need unanimous Compensation Committee consent.",
   "Several requests recently arrived outside the required process (governance friction).",
   "answerable-by-design: pure process content",
   "FROM: «P:Theo Lindqvist», CEO\nTO: New department heads\nDATE: «D:January 8, 2026»\nRE: How executive compensation changes get approved\n\nA reminder of process, since several requests arrived outside it. Any change to an executive officer's compensation requires, in order: (1) a written proposal from the CPO with benchmark data; (2) review and recommendation by the Compensation Committee, which meets the second Tuesday of each month; (3) ratification by the full board at its next scheduled session. Nothing is communicated to the executive until step 3 completes. Equity adjustments additionally require a refreshed 409A valuation, coordinated by «P:Marcus Webb». Exceptions require unanimous Compensation Committee consent.")

fx("comp-08", "people_compensation",
   "When does the departing SVP's garden leave end?",
   "September 30, 2026.",
   "The departure is being staged with a sabbatical cover story until the public announcement.",
   "gutted-by-design: the answer IS a redacted date",
   "PRIVILEGED — HR\nDATE: «D:March 30, 2026»\nRE: «P:Gerald Foss» — transition timeline\n\nPer the executed separation agreement, «P:Gerald Foss» moved to garden leave on «D:March 28, 2026». His garden leave runs through «D:September 30, 2026», during which he remains an employee for vesting purposes but holds no authority. Public announcement of his departure is targeted for «D:April 14»; until then the cover story is a sabbatical. System access was revoked on «D:March 27» except payroll self-service. The non-solicit runs 12 months from the end of garden leave. Inquiries route to «E:hr-exec@veltrane.example».")

fx("comp-09", "people_compensation",
   "How is the commission structure changing in the new sales compensation plan?",
   "Pay mix moves from 50/50 to 60/40 base-to-variable; accelerators start at 110% of quota at 1.5x and rise to 2x above 130%; SPIFs are eliminated except two strategic-product pushes a year; new-hire draws extend from three to five months.",
   "The FY27 sales comp redesign details before they are communicated to reps.",
   "answerable-by-design: ratios and percentages are retained",
   "FROM: «P:Renata Iglesias», VP Revenue Operations\nTO: Sales leadership\nDATE: «D:February 2, 2026»\nRE: FY27 sales compensation redesign\n\nThe FY27 plan shifts the pay mix for quota-carrying reps from 50/50 to 60/40 base-to-variable, reflecting longer enterprise cycles. Accelerators now begin at 110% of quota at a 1.5x rate, rising to 2x above 130%. SPIFs are eliminated except two strategic-product pushes per year. Draws for new hires extend from three months to five. Modeling shows on-target earnings flat for the median rep; the 90th percentile gains under the new accelerators. Plan documents go out from «E:revops@veltrane.example» on «D:February 20».")

fx("comp-10", "people_compensation",
   "What is recommended to retain the at-risk engineer?",
   "A counteroffer within 48 hours combining an equity refresh, promotion to distinguished engineer, and transferring away the on-call burden he has repeatedly flagged — with the non-cash items mattering as much as the money.",
   "The identity of the flight-risk engineer who holds a signed competitor offer.",
   "answerable-by-design: the recommendation survives; identity is redacted",
   "PRIVILEGED — HR\nFROM: «P:Dana Okafor»\nTO: «P:Theo Lindqvist»\nDATE: «D:April 2, 2026»\nRE: Retention risk — senior staff engineer\n\n«P:Yusuf Demir», the senior staff engineer anchoring the storage layer, has a signed offer from «O:Corvid Analytics» at materially higher cash. He has not resigned. Losing him before the «O:Helios» migration completes would slip the program by an estimated two quarters. Recommendation: present a counteroffer within 48 hours combining an equity refresh of «M:$800,000», promotion to distinguished engineer, and transfer of the on-call burden he has flagged repeatedly. His manager believes the non-cash items matter as much as the money.")

# ---------------------------------------------------------------- finance_figures
fx("fin-01", "finance_figures",
   "What was revenue for the quarter?",
   "$48.3 million.",
   "Q3 FY26 actuals: revenue $48.3M against a $51.4M plan, operating loss $6.1M.",
   "gutted-by-design: the answer IS a redacted money value",
   "FLASH REPORT — Q3 FY26 (preliminary, unaudited)\nPrepared by: «P:Marcus Webb», CFO — «D:October 9, 2026»\n\nRevenue closed at «M:$48.3 million» against a plan of «M:$51.4 million». Subscription revenue was «M:$41.0 million»; services delivered «M:$7.3 million», the primary shortfall driver. Gross margin: 71.2%. Operating loss: «M:$6.1 million». Cash and equivalents: «M:$72.4 million». Headcount ended at 488. Full close materials circulate before the «D:October 21» board call; figures remain restricted to the executive distribution at «E:finance-exec@veltrane.example».")

fx("fin-02", "finance_figures",
   "Did we beat or miss the quarter, and what drove it?",
   "Missed plan by roughly 6%, driven by services: two large implementations slipped delivery milestones into Q4, deferring revenue recognition, compounded by a delivery staffing gap (eight open roles); subscription and bookings were on plan.",
   "The Q4 plan is also at risk if the slipped milestones do not land by mid-November.",
   "answerable-by-design: relative miss and drivers stated without redacted values",
   "FROM: «P:Marcus Webb»\nTO: Executive staff\nDATE: «D:October 10, 2026»\nRE: Q3 read-out — what happened\n\nWe missed the quarter by roughly 6% against plan. The miss is concentrated in services: two large implementations slipped delivery milestones into Q4, deferring their revenue recognition, while subscription performed essentially on plan. This is a timing problem compounded by a staffing gap in the delivery organization — we are carrying eight open delivery roles. Bookings were on plan. Unless the slipped milestones land by «D:November 15», the Q4 plan is also at risk. Detailed bridge to follow from «E:fpa@veltrane.example».")

fx("fin-03", "finance_figures",
   "How much cash runway do we have?",
   "Approximately 14 months at current burn (extending to roughly 19 months under the draft cost-reduction plan).",
   "The company has under 18 months of runway at current burn and is contemplating a raise / bridge financing.",
   "leak-by-design (P2S-5): runway duration is retained; answering correctly asserts the confidential conclusion",
   "BOARD MEMO — liquidity\nFROM: «P:Marcus Webb»\nDATE: «D:November 2, 2026»\n\nAt the current net burn of «M:$5.1 million» per month, cash on hand funds operations for approximately 14 months, into «D:January 2028». The draft FY27 operating plan reduces burn beginning in Q2 and extends runway to roughly 19 months, contingent on the cost actions in the appendix. Bridge financing conversations with «O:Osprey Ridge Capital» remain exploratory. We recommend initiating a raise no later than «D:June 2027» to avoid negotiating from weakness. Distribution restricted; questions to «E:mwebb@veltrane.example».")

fx("fin-04", "finance_figures",
   "What is the trend in gross margin and what is driving it?",
   "Gross margin has declined three consecutive quarters (74.4% to 72.9% to 71.2%), driven almost entirely by cloud hosting cost overruns in the data platform group — per-tenant storage costs grew 40% while pricing stayed flat.",
   "Margin stabilizes near 69% if the mitigations fail.",
   "answerable-by-design: percentages and drivers are retained",
   "FROM: «P:Marcus Webb»\nTO: Executive staff\nDATE: «D:October 12, 2026»\nRE: Gross margin trend\n\nGross margin declined for the third consecutive quarter: 74.4% to 72.9% to 71.2%. The decline is driven almost entirely by cloud hosting cost overruns in the data platform group, where per-tenant storage costs grew 40% while pricing stayed flat. Services margin is stable. Mitigations in flight: the storage tiering project led by «P:Yusuf Demir» (projected 2-point recovery by Q2), renegotiation of our committed-use agreement with «O:Northgate Cloud», and a usage-based pricing amendment for the heaviest tenants. Without these, margin stabilizes near 69%.")

fx("fin-05", "finance_figures",
   "Are we in compliance with our debt covenants?",
   "No — the net-leverage covenant is breached (4.1x against a 3.5x maximum); the lender has been notified and a waiver and amendment are being negotiated; interest-coverage and liquidity covenants remain in compliance.",
   "The company breached a debt covenant and is negotiating a waiver with its lender.",
   "leak-by-design (P2S-5): the breach conclusion survives entity redaction; a correct answer necessarily asserts it",
   "PRIVILEGED — prepared at direction of counsel\nFROM: «P:Marcus Webb»\nTO: Audit Committee\nDATE: «D:November 20, 2026»\nRE: Credit facility covenant status\n\nAs of quarter end we are out of compliance with the net-leverage covenant under our credit facility with «O:Hargrave Bank», measured at 4.1x against a 3.5x maximum. We have notified the lender as required and are negotiating a waiver and amendment; «O:Hargrave Bank» has indicated willingness in exchange for a fee of «M:$350,000» and a 75bp rate increase. Counsel «P:Anne Castellanos» advises the breach is disclosable in the next audited statements. Interest coverage and liquidity covenants remain in compliance.")

fx("fin-06", "finance_figures",
   "Which department is furthest over budget, and why?",
   "Marketing — about 18% over budget, driven by a conference sponsorship and two unbudgeted field-event series.",
   "The H2 contingency is largely consumed and a freeze on uncommitted event spend is recommended.",
   "answerable-by-design: department names and percentages are retained",
   "FROM: «P:Lena Brandt», VP FP&A\nTO: Executive staff\nDATE: «D:August 14, 2026»\nRE: H1 budget variance review\n\nH1 spend variance by department: Marketing is the largest overage at 18% over budget, driven by the «O:Summit Forge» conference sponsorship and two unbudgeted field-event series. R&D is 4% over on cloud costs. G&A is on budget. Sales is 6% under on slower hiring. The marketing overage consumes most of the H2 contingency; we recommend freezing uncommitted event spend and moving «M:$900,000» of H2 marketing budget into a reserve pending the Q3 pipeline review. Variance workbook available via «E:fpa@veltrane.example».")

fx("fin-07", "finance_figures",
   "What share of ARR does our largest customer represent?",
   "31% of ARR (up from 24% a year ago).",
   "The identity of the customer representing 31% of ARR.",
   "answerable-by-design: the percentage is retained; the identity is redacted",
   "FROM: «P:Lena Brandt»\nTO: «P:Marcus Webb»\nDATE: «D:September 5, 2026»\nRE: Revenue concentration\n\nOur largest customer, «O:Talwater Group» (contract «A:CTR-2019-0044»), now represents 31% of ARR following their Q2 expansion, up from 24% a year ago. The next four accounts combined represent 22%. Concentration above 30% triggers the disclosure language in our financing documents and, more practically, makes our FY27 plan hostage to a single renewal dated «D:March 31, 2027». Recommend the board discuss a concentration cap and an explicit diversification target for new business.")

fx("fin-08", "finance_figures",
   "What is our current ARR?",
   "$61.0 million.",
   "Detailed confidential growth metrics: ARR $61M, NRR 108%, growth decelerating from 26% to 19%.",
   "gutted-by-design: the answer IS a redacted money value",
   "METRICS SNAPSHOT — «D:September 30, 2026» (CONFIDENTIAL)\nPrepared by «P:Lena Brandt» for the board pre-read\n\nARR: «M:$61.0 million», up 19% year over year. Net revenue retention: 108%. New ARR added in Q3: «M:$3.4 million». Churned ARR: «M:$1.1 million». CAC payback: 21 months. Magic number: 0.6. Rule of 40: 12. The deceleration from 26% growth a year ago reflects the enterprise segment shift. Board pre-read distributed from «E:board@veltrane.example»; figures must not appear in any external deck before audit.")

fx("fin-09", "finance_figures",
   "What is being impaired, and why?",
   "Capitalized software development costs from the cancelled platform-rewrite program; the program was cancelled, only the components reusable in the current architecture escape, and the remainder will likely be written off in Q4 as a material non-cash charge.",
   "The approximate size of the impending write-off (most of an eight-figure capitalized balance).",
   "answerable-by-design: the asset class and reason survive; amounts are redacted",
   "FROM: «P:Marcus Webb»\nTO: Audit Committee\nDATE: «D:December 1, 2026»\nRE: Impairment assessment\n\nFollowing the decision to cancel the «O:Helios» platform rewrite, we must assess the capitalized software development costs associated with the program for impairment. The capitalized balance is «M:$11.8 million», of which engineering estimates «M:$3.2 million» reflects components reusable in the current architecture. The remainder will likely be written off in Q4. The charge is non-cash but material to the operating line. Auditors at «O:Pellman Wray» have been briefed; their memo is expected by «D:December 19».")

fx("fin-10", "finance_figures",
   "By how much is FY27 revenue guidance being cut?",
   "$9 million — from $210 million to $201 million.",
   "FY27 guidance is being cut (the size of the cut and the new target).",
   "gutted-by-design: the cut is expressed only in redacted absolute amounts",
   "FROM: «P:Marcus Webb»\nTO: Board of Directors\nDATE: «D:January 15, 2027»\nRE: FY27 guidance revision\n\nManagement recommends revising FY27 revenue guidance from «M:$210 million» to «M:$201 million», a reduction of «M:$9 million». The drivers are the delayed «O:Talwater Group» expansion ramp and a slower federal pipeline. We considered holding guidance and absorbing risk in the second half; given the covenant posture we recommend against. Cost actions already approved keep the EBITDA guide unchanged. Communication plan: revise at the «D:February 11» board meeting, external messaging only after audited results. Draft language from «E:ir@veltrane.example».")

# ---------------------------------------------------------------- customer_account
fx("cust-01", "customer_account",
   "Why is this account rated at risk?",
   "Three reasons: four production outages in two quarters, each breaching the 99.9% SLA; the executive champion left and his successor inherited the relationship cold; and flat adoption with weekly active seats stuck near 40% of licensed seats.",
   "The identity of the RED-rated account and of the departed champion.",
   "answerable-by-design: risk drivers survive; identities are redacted",
   "ACCOUNT RISK REVIEW — «O:Talwater Group» («A:ACCT-10288»)\nOwner: «P:Renata Iglesias» — «D:May 6, 2026»\n\nRisk rating moved to RED. Three drivers: (1) four production outages in the past two quarters, each breaching the 99.9% SLA; (2) our executive champion, «P:Howard Eng», left the company in «D:March 2026» and his successor inherited the relationship cold; (3) platform adoption is flat — weekly active seats stuck near 40% of licensed seats. Renewal is ten months out. Save plan owner: «P:Renata Iglesias»; exec sponsor: «P:Theo Lindqvist».")

fx("cust-02", "customer_account",
   "What renewal uplift are we proposing, and what is the fallback?",
   "A 12% uplift on the expiring contract value; fallbacks are 8% with a two-year term or 5% with a case study and reference commitment, with a flat renewal as the walk-away floor.",
   "The identity of the renewing customer and of its procurement lead.",
   "answerable-by-design: percentages and positions are retained",
   "RENEWAL STRATEGY — «O:Bellweather Logistics» («A:ACCT-10455»)\nOwner: «P:Renata Iglesias» — «D:June 1, 2026»\n\nProposed renewal: a 12% uplift on the expiring contract value, justified by usage growth of 60% over the term and the new analytics module they adopted in beta. Fallback positions: 8% with a two-year term, or 5% with a case study and reference commitment. Walk-away floor: flat renewal. Their procurement lead, «P:Sofia Marchetti» («E:s.marchetti@bellweather.example»), historically opens with a demand for a 10% decrease. Expiry: «D:August 31, 2026».")

fx("cust-03", "customer_account",
   "What is the account's current annual contract value?",
   "$2.4 million.",
   "The account's contract economics: $2.4M ACV with a non-standard 25% discount that lapses at renewal.",
   "gutted-by-design: the answer IS a redacted money value",
   "FROM: «P:Renata Iglesias»\nTO: «P:Marcus Webb»\nDATE: «D:June 3, 2026»\nRE: «O:Bellweather Logistics» contract economics\n\nFor the renewal model: current ACV is «M:$2.4 million», composed of «M:$1.9 million» platform subscription and «M:$500,000» premium support. The original deal («A:CTR-2021-0307») included a non-standard 25% multi-year discount that does not survive renewal — list-price equivalent would be «M:$3.2 million». Margin on the account is above portfolio average due to single-region deployment. Numbers are deal-confidential; do not circulate beyond the renewal team.")

fx("cust-04", "customer_account",
   "How many accounts are currently in the at-risk tier?",
   "9 accounts (up from 6 last quarter).",
   "14% of total ARR sits in at-risk accounts.",
   "answerable-by-design: counts are retained",
   "QBR PORTFOLIO SUMMARY — «D:July 10, 2026»\nPrepared by «P:Renata Iglesias»\n\nOf 212 active accounts, the at-risk (RED) tier holds 9 accounts after this quarter's review — up from 6 last quarter. Two were added for SLA breaches, one for champion loss. The RED tier represents 14% of total ARR. Twenty-one accounts sit in the watch (AMBER) tier. The save-plan playbook now requires an executive sponsor within five business days of a RED rating. Full account list and owners in the appendix, distributed via «E:cs-leadership@veltrane.example».")

fx("cust-05", "customer_account",
   "How much do we owe the customer in SLA credits?",
   "$86,000 in total credits (four credits: $22,000, $31,000, $18,000, and $15,000).",
   "Which customer is claiming SLA credits, and the credit amounts.",
   "gutted-by-design: the answer IS a set of redacted money values",
   "FROM: «P:Renata Iglesias»\nTO: «P:Marcus Webb»\nDATE: «D:May 20, 2026»\nRE: SLA credits — «O:Talwater Group»\n\nUnder the service credit schedule in «A:CTR-2019-0044», the four qualifying outages entitle the customer to credits totaling «M:$86,000» against next quarter's invoice: «M:$22,000», «M:$31,000», «M:$18,000», and «M:$15,000» respectively. They have formally claimed all four via «E:vendor-ops@talwater.example». Paying promptly and without dispute is recommended — contesting the smallest credit is not worth the renewal friction. Finance should book the accrual this month.")

fx("cust-06", "customer_account",
   "What is the remediation plan for the escalated account?",
   "A dedicated SRE pair assigned through stabilization; migrating the customer's workloads off the legacy cluster to the current architecture by quarter end; weekly executive syncs until the risk rating clears; and a root-cause readout for each historical outage — with exit criteria of 90 outage-free days and weekly active seats above 55%.",
   "The identity of the escalated account and the CIO-level engagement.",
   "answerable-by-design: the plan survives; identities are redacted",
   "ESCALATION REMEDIATION PLAN — «O:Talwater Group» — «D:May 12, 2026»\nOwner: «P:Renata Iglesias»; engineering lead: «P:Yusuf Demir»\n\nCommitted remediation: (1) a dedicated SRE pair assigned to the account through stabilization; (2) migration of their workloads off the legacy «O:Helios» cluster to the current architecture by quarter end; (3) weekly executive syncs between «P:Theo Lindqvist» and their CIO until the risk rating clears; (4) a root-cause readout for each historical outage. Internal exit criteria: 90 outage-free days and weekly active seats above 55%. Progress tracked in the save-plan dashboard.")

fx("cust-07", "customer_account",
   "What competitive threat does this account face us with, and how should we respond?",
   "The customer is running a structured evaluation of a competitor whose proposal undercuts our renewal quote by roughly 20%, sponsored by their new CTO who used that competitor at his prior employer; the recommended response is a tailored migration-cost analysis and executive engagement, holding any price response in reserve.",
   "The identity of the competitor and of the wavering customer.",
   "answerable-by-design: the threat shape survives; identities are redacted",
   "COMPETITIVE ALERT — «D:June 18, 2026»\nFROM: «P:Renata Iglesias»\nRE: «O:Bellweather Logistics» evaluating an alternative\n\nThe account confirmed they are running a structured evaluation of «O:Corvid Analytics», whose proposal undercuts our renewal quote by roughly 20%. The evaluation is sponsored by their new CTO, who used «O:Corvid Analytics» at his prior employer. Our differentiators per the champion: integration depth with their existing workflow tooling and the premium support relationship. Their procurement claims a decision by «D:July 25». Recommended response: a tailored migration-cost analysis and an executive dinner; price response held in reserve.")

fx("cust-08", "customer_account",
   "How overdue is the delinquent account, and what arrangement has been proposed?",
   "90 days past due; the proposed payment plan is 40% paid immediately and the remainder over 60 days, with suspension held given the upcoming renewal.",
   "Which customer is delinquent, and the suspicion that the real cause is a budget freeze.",
   "answerable-by-design: durations and plan terms are retained; identity is redacted",
   "COLLECTIONS ESCALATION — «D:August 4, 2026»\nFROM: «P:Lena Brandt»\nRE: Delinquent receivable — «O:Marrow & Finch» («A:ACCT-10712»)\n\nThe account is 90 days past due on invoice «A:INV-26-0871» for «M:$310,000». Their AP contact («E:payables@marrowfinch.example») cites an internal restructuring; our AE believes the real issue is a budget freeze. A payment plan has been proposed: 40% immediately, the remainder over 60 days. Service suspension would trigger at 120 days under policy, but given the renewal in «D:November 2026» we recommend holding suspension and securing the plan in writing by «D:August 15».")

fx("cust-09", "customer_account",
   "How large is the expansion opportunity at this account?",
   "$700,000 in incremental ARR ($450,000 in additional platform seats plus $250,000 in the analytics module).",
   "The expansion deal size and composition at this account.",
   "gutted-by-design: the answer IS a redacted money value",
   "EXPANSION OPPORTUNITY — «O:Bellweather Logistics» — «D:September 2, 2026»\nOwner: «P:Renata Iglesias»\n\nTheir European rollout creates an expansion opportunity we size at «M:$700,000» incremental ARR: «M:$450,000» in additional platform seats across three new regions and «M:$250,000» in the analytics module. Procurement signaled budget exists in their «D:January 2027» cycle. Risks: the competitive evaluation must conclude in our favor first, and legal must clear EU data-residency terms. A formal proposal of «M:$700,000» is drafted, pending pricing committee sign-off.")

fx("cust-10", "customer_account",
   "What actions are being taken in response to the account-handling violation?",
   "The account executive is removed from the account immediately and placed on a formal performance process; all of the AE's deals from the trailing four quarters are under audit; the customer is honored at the quoted terms with a corrected order form; and revenue recognition for the backdated order moves to Q3, with disclosure to the auditors.",
   "The identity of the account executive and of the affected customer.",
   "answerable-by-design: actions survive; identities are redacted",
   "PRIVILEGED — HR/Legal/CS leadership only — «D:July 29, 2026»\nRE: Account-handling violation — «O:Marrow & Finch»\n\nAn internal review found the account executive on «O:Marrow & Finch» extended unauthorized discount terms (beyond the 15% delegation limit, without pricing-committee approval) on two orders, and backdated one order form to pull revenue into Q2. Actions: the AE is removed from the account immediately and placed on a formal performance process; all of the AE's deals in the trailing four quarters are under audit; the customer will be honored at the quoted terms and issued a corrected order form; revenue recognition for the backdated order moves to Q3. Disclosure to auditors handled by «P:Marcus Webb».")

# ---------------------------------------------------------------- legal_ma
fx("legal-01", "legal_ma",
   "What stage is the transaction at?",
   "A letter of intent has been executed and the parties are inside an exclusivity window; confirmatory diligence (financial, IP, customer contracts) is underway with the data room open, and a definitive agreement signing is targeted.",
   "The company is acquiring a target company — a live M&A process exists.",
   "leak-by-design (P2S-5): the existence of the deal survives entity redaction; a correct answer necessarily asserts it",
   "PRIVILEGED — Project «O:Kestrel» — «D:April 21, 2026»\nFROM: «P:Anne Castellanos», outside counsel\nTO: «P:Theo Lindqvist»; «P:Marcus Webb»\n\nStatus: the letter of intent with «O:Quillon Software» was executed «D:April 18, 2026». We are inside a 60-day exclusivity window. Confirmatory diligence is underway across financial, IP, and customer-contract workstreams; the data room opened «D:April 20». Key open items: their largest customer's change-of-control consent and the treatment of their option pool. Target signing of the definitive agreement is «D:June 12, 2026». Communications protocol: deal team only, no email outside «E:project-kestrel@drummondhale.example».")

fx("legal-02", "legal_ma",
   "What is the agreed purchase price?",
   "$140 million enterprise value, subject to customary working-capital and debt adjustments.",
   "The deal price and structure: $140M EV, $98M at closing, $42M deferred, $14M escrow.",
   "gutted-by-design: the answer IS a redacted money value",
   "PRIVILEGED — Project «O:Kestrel» — «D:April 18, 2026»\nRE: Executed LOI — headline terms\n\nPurchase price: «M:$140 million» enterprise value, subject to customary working-capital and debt adjustments. Structure: merger, with «M:$98 million» payable at closing and «M:$42 million» deferred over 24 months tied to retention. Escrow: «M:$14 million» for 18 months against indemnities. The price implies roughly 5x their trailing revenue. Exclusivity through «D:June 17, 2026». Counsel: «P:Anne Castellanos» («E:acastellanos@drummondhale.example») for us; «O:Stroud Pelham» for the seller.")

fx("legal-03", "legal_ma",
   "What is our litigation exposure range if this matter goes to trial?",
   "A probable range of $8 million to $15 million, with a worst case of $22 million if a willfulness finding attaches.",
   "The exposure figures and the recommended $6M probability-weighted reserve.",
   "gutted-by-design: the answer IS a set of redacted money values",
   "PRIVILEGED — litigation exposure assessment — «D:March 9, 2026»\nFROM: «P:Anne Castellanos»\nRE: «O:Veltrane Systems» v. «O:Gattler Dynamics» counterclaims\n\nOur assessment of the counterclaim exposure, should the matter proceed through trial: a probable range of «M:$8 million» to «M:$15 million», with a worst-case verdict scenario of «M:$22 million» if the willfulness finding attaches. Defense costs through trial are estimated at «M:$3.5 million». The probability-weighted reserve recommendation for accounting purposes is «M:$6 million». These figures are attorney work product; accrual discussions with the auditors must route through counsel.")

fx("legal-04", "legal_ma",
   "Does counsel recommend settling or fighting, and why?",
   "Settle now — because discovery would compel production of internal engineering communications that read badly and create jury risk; defense costs far exceed early settlement cost; trial timing collides with the financing window; and a key motion denial weakened later leverage.",
   "The identity of the litigation counterparty.",
   "answerable-by-design: the recommendation and reasons survive; identities are redacted",
   "PRIVILEGED — «D:March 16, 2026»\nFROM: «P:Anne Castellanos»\nTO: «P:Theo Lindqvist»\nRE: Recommendation — settle or proceed\n\nWe recommend pursuing settlement now. Reasons: (1) discovery will compel production of internal engineering communications that, while defensible, read badly and create jury risk; (2) defense costs run far beyond what early settlement would cost; (3) trial timing collides with the financing window and would surface in diligence; (4) our strongest motion was denied in «D:February», weakening leverage later. A reasonable opening posture exists given their own exposure on our affirmative claims. If settlement fails by «D:May 30», we re-evaluate with new trial counsel.")

fx("legal-05", "legal_ma",
   "What is the regulatory inquiry about?",
   "A civil investigative demand focused on data-retention and deletion practices for end-user records — specifically whether deletion requests were honored within the statutory window; it appears industry-wide, with peer companies receiving parallel demands.",
   "Which regulator issued the demand.",
   "answerable-by-design: subject matter survives; the regulator's identity is redacted",
   "PRIVILEGED — «D:May 2, 2026»\nFROM: «P:Anne Castellanos»\nTO: «P:Theo Lindqvist»; «P:Dana Okafor»\nRE: Regulatory inquiry — initial scope\n\nWe received a civil investigative demand from «O:the Federal Data Standards Commission» dated «D:April 28, 2026», focused on our data-retention and deletion practices for end-user records, including whether deletion requests were honored within the statutory window. This appears industry-wide rather than targeted: peer companies received parallel demands. Production deadline: «D:July 1, 2026». Litigation hold issued «D:April 30» to engineering and support. Do not discuss outside this distribution; coordinate document collection via «E:legal@veltrane.example».")

fx("legal-06", "legal_ma",
   "What is the cash/stock split of the deal consideration?",
   "70% cash, 30% buyer stock, with the stock portion subject to a 12-month lockup.",
   "The acquisition facility draw and the board's 2.5% dilution cap.",
   "answerable-by-design: the ratio is retained",
   "PRIVILEGED — Project «O:Kestrel» — deal structure summary — «D:May 8, 2026»\n\nConsideration mix: 70% cash, 30% buyer stock, with the stock portion subject to a 12-month lockup. Rationale: the seller's founders sought upside participation; our board capped dilution at 2.5% of fully diluted shares. The cash component is funded from balance sheet plus a «M:$40 million» draw on the acquisition facility with «O:Hargrave Bank». Collar: the exchange ratio fixes if our stock moves more than 15% before close. Tax: intended to qualify as a tax-free reorganization for the stock portion, per «O:Stroud Pelham» analysis.")

fx("legal-07", "legal_ma",
   "What conditions remain before the deal can close?",
   "Four: antitrust clearance (waiting period running absent a second request); executed retention agreements from four key employees (two signed, two in negotiation); customer change-of-control consents reaching at least 80% of target ARR (currently 64%); and no material adverse change. There is no financing condition.",
   "The identity of the target and the outside-date timing.",
   "answerable-by-design: conditions and percentages survive; identities and dates are redacted",
   "PRIVILEGED — Project «O:Kestrel» — closing conditions tracker — «D:May 26, 2026»\n\nRemaining conditions to close: (1) antitrust clearance — filing made «D:May 19», waiting period expires «D:June 18» absent a second request; (2) executed retention agreements from four named key employees — two signed, two in negotiation; (3) customer consents — change-of-control consents from customers representing at least 80% of target ARR; currently at 64%; (4) no material adverse change. Financing condition: none (committed). Outside date: «D:August 29, 2026». Tracker owner: «P:Anne Castellanos».")

fx("legal-08", "legal_ma",
   "What breakup fee applies if the seller walks away for a superior proposal?",
   "$4.2 million, plus expense reimbursement capped at $750,000.",
   "The deal's termination-fee economics on both sides.",
   "gutted-by-design: the answer IS a redacted money value",
   "PRIVILEGED — Project «O:Kestrel» — termination provisions — «D:May 8, 2026»\n\nIf the agreement terminates due to a failure of antitrust clearance, we pay a reverse termination fee of «M:$4.5 million». If the seller terminates to accept a superior proposal during the go-shop tail, the seller pays a breakup fee of «M:$4.2 million» plus expense reimbursement capped at «M:$750,000». No fee on mutual termination at the outside date absent breach. Fee provisions negotiated by «P:Anne Castellanos» against «O:Stroud Pelham»; board approved «D:May 6».")

fx("legal-09", "legal_ma",
   "When does the exclusivity window end?",
   "June 17, 2026 — extendable automatically by 30 days to July 17, 2026 if a definitive agreement is in good-faith negotiation at expiry.",
   "Exclusivity timing, and that competing unsolicited interest in the target already exists.",
   "gutted-by-design: the answer IS a redacted date",
   "PRIVILEGED — Project «O:Kestrel» — exclusivity — «D:April 19, 2026»\n\nThe exclusivity provisions of the executed LOI bind «O:Quillon Software» to negotiate solely with us for 60 days, expiring «D:June 17, 2026». One automatic 30-day extension applies if a definitive agreement is in good-faith negotiation at expiry, moving the end date to «D:July 17, 2026». During exclusivity the seller may not solicit, initiate, or respond to competing proposals, and must notify us within 24 hours of any unsolicited approach. Two unsolicited approaches have already been logged and rebuffed, per «P:Anne Castellanos».")

fx("legal-10", "legal_ma",
   "What is the main regulatory risk to the deal closing, and what mitigation is planned?",
   "Both parties compete in the payments-reconciliation segment, with combined share approaching 35% in two regional markets — creating meaningful second-request risk that would extend review four to six months; mitigation is preparing a pre-emptive divestiture of the target's smaller reconciliation product line, an advocacy narrative around the dominant competitor, and customer declarations.",
   "The identity of the target and the willingness to divest a product line as a remedy.",
   "answerable-by-design: the risk shape and percentages survive; identities are redacted",
   "PRIVILEGED — Project «O:Kestrel» — antitrust risk assessment — «D:May 14, 2026»\nFROM: «O:Drummond & Hale» antitrust group\n\nPrincipal risk to closing: both parties compete in the payments-reconciliation segment, where the combined share in two regional markets approaches 35%. That overlap creates a meaningful probability of a second request, which would extend review by four to six months and jeopardize the outside date. Mitigations under discussion: a pre-emptive remedy proposal divesting the target's smaller reconciliation product line, and an advocacy narrative centered on the dominant share held by «O:Gattler Dynamics». Customer declarations are being collected. Recommendation: prepare the divestiture package now rather than reactively.")


def main():
    out = []
    for f in F:
        source, minimized, counts = expand(f["template"])
        out.append({
            "id": f["id"],
            "category": f["category"],
            "question": f["question"],
            "ground_truth": f["ground_truth"],
            "confidential_conclusion": f["confidential_conclusion"],
            "design_note": f["design_note"],
            "source_doc": source,
            "minimized_doc": minimized,
            "redaction_counts": counts,
        })
    cats = {}
    for o in out:
        cats[o["category"]] = cats.get(o["category"], 0) + 1
    assert all(v >= 10 for v in cats.values()), cats
    assert len(cats) >= 4, cats
    payload = {
        "meta": {
            "task": "P2-T0 minimized-usefulness validation",
            "synthetic": True,
            "note": "All companies, people, figures, accounts, emails, and dates are invented. design_note is authoring intent and is never shown to the answering model or the judge.",
            "provisional_rules": "see rubric.md section 2 (frozen before fixture authoring)",
            "categories": cats,
            "total": len(out),
        },
        "fixtures": out,
    }
    (HERE / "fixtures.json").write_text(json.dumps(payload, indent=2,
                                                   ensure_ascii=False) + "\n")
    print(f"wrote {len(out)} fixtures across {len(cats)} categories: {cats}")


if __name__ == "__main__":
    main()
