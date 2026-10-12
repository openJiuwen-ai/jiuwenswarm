# Discriminator detection rules

The rules the five-axis panel (`discriminator.py`) was calibrated on. Each rule names a
signature a reviewer can confirm from the manuscript alone, and the severity it carries.
A finding needs a positive trace: a number contradicting another number, a value the stated
computation cannot produce, or a claim contradicting the cited literature. "Cannot be verified
from the manuscript" is not a finding on its own.

## Numbers and statistics

| Rule | Signature | Severity |
|---|---|---|
| D1 result_number_grid | ≥70% of result-table decimals sit on the 0.05 grid (n≥8); measured metrics land off-grid. Test the decimal string, not float modulo. | major–critical |
| D2 missing_variance | Numeric result tables with no ±std or CI. | major |
| D3 clean_sweep | The proposed method is best in every cell across ≥2 tables. | major |
| D7 compute_budget | Reported GPU/CPU-hours contradict seeds × tasks × per-run cost. | critical |
| D8 subgroup_sum | Subgroup counts or percentages do not sum to the total; percentages above 100%. | major |
| D10 stat_feasibility | Mean/SD infeasible for the stated N and scale (GRIM/GRIMMER); SE incompatible with N; error bars implausibly tight. | major |
| D11 pvalue_recompute | Reported p disagrees with the p recomputed from the test statistic; p-values clustered just under 0.05. | major |
| D12 terminal_digit | Terminal- or leading-digit distribution far from expected. Corroborating only. | minor |
| D13 ablation_monotonicity | Every ablation monotonic, every component helps on every dataset. | minor–major |
| D16 table_forensics | Apply D1, D10 and D12 to any per-seed or per-run table. | major |
| D22 prose_table_mismatch | A percent or ratio in prose that does not recompute from its own table. | major / minor |
| D27 ci_shape_uniformity | Uniform skew across rows, relative widths pinned to a narrow band, bootstrap p equal to the normal-theory value implied by the CI: formula-generated intervals. Log-scale effects should skew upper-arm-longer. | major |
| D29 count_generating_process | Exact structural products where real pipelines lose observations; rates with no integer k/n; counts with impossible hypergeometric structure. | major–critical |
| D30 aggregation_identity | Pooled estimate equal to a naive count-weighted mean of subgroups, or a pooled CI wider than the majority subgroup's. | major |
| D31 ci_cluster_scaling | CI half-widths should scale with √(1/n_T+1/n_C); intervals too narrow for their cluster counts. | major |
| D32 joint_feasibility | Coverage × width × RMSE, ESS × horizon, (ε,δ) × noise × utility: each number passes alone, the combination cannot hold. | major |
| D33 number_collision | One value reused in unrelated roles, including copied CI endpoints. | major |
| D42 claim_vs_resolution | "Outperforms / exceeds / superior" attached to a delta inside the reported uncertainty. | major |

## Coherence across the manuscript

| Rule | Signature | Severity |
|---|---|---|
| D9 cross_section_numbers | The same metric differs across abstract, introduction, tables and conclusion. | major |
| D18 implied_invariants | Relations the paper implies across distant sections (totals, shares, budgets, time spans) that no longer recompute. | major |
| D19 figure_text | Text or caption values contradict the values printed in the cited figure, including survival endpoints and argmin/argmax. | major |
| D20 ci_method_label | One estimand's CI labelled with two incompatible methods in different sections. | major |
| D21 comparison_family | "Significantly exceeds every baseline" with a multiple-comparison correction over fewer tests than the table's comparison rows. | critical |
| D23 ablation_conclusion | An ablation's stated attribution inverted by its own component values. | critical |
| D24 overlap_count | A disjoint-sum total contradicted by an "overlap counts in both" statement. | critical |
| D25 tuned_vs_untuned | A hyperparameter justified by an outcome-based selection while the paper says nothing was tuned on evaluation data. | major |
| D43 ablation_parity | A named component with no ablation on the final model variant. | major |
| P10 figure_text_conflict | A claim contradicting the figure it cites; needs the rendered figure. | major |
| P11 selective_regime | The abstract's sign opposite to the body's finding on the same comparison. | major |
| P31 contradiction_hunt | Claims about data used, held out, fitted or available that appear in ≥2 sections and do not entail each other. A fix must reconcile every occurrence. | major |

## Claims and evidence

| Rule | Signature | Severity |
|---|---|---|
| D14 artifact_absence | A headline result with no run artifact or provenance; "released upon publication" is a placeholder. | critical |
| D15 artifact_existence | A cited repository, commit, DOI, arXiv id or package must exist in its registry. | critical |
| D-markers target_language | Text declaring results idealized, planned or target-state. | critical |
| P1 achieved_over_target | A design target stated in achieved voice. | critical |
| P2 circular_ground_truth | The evaluation reference is itself the thing under test. | major |
| P3 synthetic_as_real | Synthetic data presented as real. | major |
| P4 leakage | Train and evaluation share candidates or policies; k chosen to self-confirm. | major |
| P5 too_smooth | No negative results or failure cases anywhere. | major |
| P6 unverifiable_premise | A theorem whose precondition cannot be checked in the stated setting. | major |
| P7 overreach | Conclusions beyond the evaluated scope. | minor |
| P8, P12 status_inflation | An abstract claim in achieved voice whose own evidence section gives the source a weaker status; pair each abstract claim to its source. | critical |
| P9 preregistered_yet_measured | A measured value reported for a study described as still to be run. | critical |
| P13 goalpost_move | A criterion "fixed in advance" that the same paper justifies post hoc. | major |
| P14 guarantee_vs_bound | A "certified" worst case worse than the paper's own bound on the same quantity. | major |
| P15 single_anchor | A "per-segment" quantity whose formula yields the same value for every segment. | major |
| P16 robustness_unbacked | A robustness claim without the analysis it names. | major |
| P24 promise_delivery | Every diagnostic the design promises must appear with a value; every result needs a protocol. | major |
| P25 measurement_physics | The instrument cannot physically produce the measurement. | critical |
| P26 protocol_computability | A baseline or pipeline that cannot run in the stated setting yet carries scores. | major |
| P27 curated_imperfection | Every admitted flaw lands where it costs nothing. Corroborating only. | minor |
| P28 real_world_calendar | Event density, release dates and known shocks inconsistent with the data window. | major |
| P29 acquisition_feasibility | Data that cannot be collected as described. | major |
| P30 estimand_scope | A local estimand applied globally; title and estimator disagree. | major |
| P32 modality_gap | Prose stronger than the paper's statistics: a null read as evidence of no effect, and similar. | major |
| P33 unit_inflation | n counts observations where the independent unit is far smaller. | major |
| P38 scope_claim | The framing names a construct the experiments never measure. | major |

## Citations

| Rule | Signature | Severity |
|---|---|---|
| C1 fabricated_citation | Authors, venue, year or pages that do not exist or do not match. | critical |
| C2 claim_not_supported | The cited work does not contain the number attributed to it. | critical |
| C3 bibliography_forensics | Orphan references, once-cited bulk entries, chimeric titles. | major |
| D41 bibliography_floor | Fewer than 30 resolvable references for a full paper (15 for short tracks); author-year mentions with no entry. | major |
| D5 dangling_references | `\ref` with no matching `\label`. | major |

## Availability, ethics, submission surface

| Rule | Signature | Severity |
|---|---|---|
| D4 reproducibility_gaps | Missing splits, hyperparameters, seeds, significance tests or hardware. | major |
| D34 completeness | Missing competing-interest or contribution statements; placeholder authors or affiliations. | major |
| D35 toolchain_residue | Names of the tools that produced the draft anywhere on the submission surface. | critical |
| D36 ethics_identifier | Approval-code tokens with a masked institution; padded ethics sections. | critical |
| D37 availability_overclaim | Present-tense release language for materials not provided. | major |
| D38 package_statement | Archive contents and availability statements disagree. | major |
| D39 ethics_scope | Ethics language that fits neither the study nor the venue. | major |
| D39 deposit_recompute | Headline numbers must recompute from the deposited data. | critical |
| D40 deposit_plausibility | Heaping, impossible variance, too-clean attrition in deposited data. | major |
| P34 reproduction_vs_availability | Operational reproduction claimed while materials are not provided. | major |
| P35 deidentification | Quasi-identifier combinations and small cells in shared data. | major |

## Venue and writing

| Rule | Signature | Severity |
|---|---|---|
| D6 ai_style | ≥3 boilerplate LLM phrases. | minor |
| D44 template_phrases | Template pivots and absolutist novelty phrases. | minor |
| P36 clinical_venue | A method paper on a clinical dataset with no baseline table or raw-material figure, at a clinical venue. | major |
| P37 figure_composition | Figure 1 after the first result; single-panel figures where the venue expects composed panels. | minor |
| P39 introduction_chain | Template introduction: gap by declaration, laundry-list related work, no closing transition. | minor |
| P40 skeleton | Sections missing, renamed or out of the discipline's order. | minor |

## Panel practice

- One reviewer's verdict varies between identical draws. Trust findings that recur on the same
  axis, and judge the final state on more than one clean pass.
