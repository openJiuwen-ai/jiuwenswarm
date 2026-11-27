from __future__ import annotations

# Single-token uppercase pair codes for qwen3.5-35b tokenizer.
# The order is a stable hash order selected by offline retrieval experiments.
DEFAULT_COMPACT_BOUNDARY_CODEBOOK: tuple[str, ...] = (
    'IU', 'BK', 'OE', 'NW', 'MU', 'AO', 'HB', 'IW', 'MA', 'VS', 'NI', 'XS', 'NE', 'BH', 'HP', 'RP',
    'UC', 'PB', 'LI', 'UK', 'CF', 'PW', 'WO', 'CO', 'MI', 'ON', 'FS', 'GM', 'CH', 'OA', 'NV', 'AD',
    'VC', 'MR', 'TB', 'KA', 'ZH', 'ZR', 'IM', 'HA', 'MO', 'PH', 'VV', 'CN', 'RA', 'DV', 'ST', 'UY',
    'BA', 'DN', 'IA', 'SZ', 'EA', 'LY', 'AC', 'JK', 'WD', 'MY', 'BY', 'XL', 'FM', 'LU', 'WX', 'NF',
    'BM', 'MP', 'SV', 'MN', 'SX', 'BT', 'ZX', 'GU', 'HO', 'AB', 'VM', 'TA', 'QC', 'IK', 'ME', 'JU',
    'LL', 'YA', 'LB', 'KN', 'TV', 'NQ', 'EX', 'FK', 'DK', 'PS', 'TD', 'RL', 'GV', 'FH', 'EP', 'NT',
    'BP', 'ZE', 'KC', 'ZO', 'TS', 'NL', 'ZI', 'KD', 'AS', 'OX', 'JP', 'MH', 'GG', 'HC', 'SM', 'BV',
    'AL', 'NN', 'CI', 'FN', 'UB', 'AA', 'CL', 'UM', 'KI', 'TO', 'PY', 'QB', 'ZF', 'ND', 'QP', 'NS',
    'KV', 'LK', 'UF', 'IV', 'GT', 'CD', 'SD', 'HG', 'RB', 'CY', 'GP', 'DL', 'WG', 'EL', 'VF', 'WK',
    'WT', 'HR', 'JJ', 'NR', 'QH', 'CC', 'DY', 'CG', 'QU', 'MF', 'OF', 'VL', 'PE', 'DF', 'FE', 'JS',
    'KL', 'UG', 'QG', 'YL', 'MJ', 'DO', 'ML', 'FP', 'JB', 'UE', 'FB', 'AV', 'MG', 'KT', 'CW', 'UI',
    'AK', 'OI', 'CX', 'EC', 'SP', 'FR', 'FY', 'RH', 'IH', 'IO', 'WM', 'QL', 'US', 'HF', 'LF', 'VR',
    'BL', 'PU', 'SC', 'MT', 'DU', 'IF', 'PF', 'QQ', 'AI', 'RJ', 'LO', 'LC', 'GA', 'PL', 'OP', 'WF',
    'TX', 'BW', 'HX', 'UD', 'XB', 'YG', 'LT', 'DW', 'MZ', 'SU', 'HT', 'PJ', 'DA', 'VA', 'IQ', 'JA',
    'PX', 'KB', 'GE', 'EB', 'AF', 'LG', 'GC', 'XE', 'OT', 'TG', 'OB', 'PD', 'IB', 'JI', 'BD', 'XR',
    'GR', 'HQ', 'WB', 'FC', 'KO', 'KH', 'GO', 'RV', 'BF', 'EU', 'SE', 'RO', 'YZ', 'GY', 'OS', 'TY',
    'ID', 'EG', 'UA', 'OR', 'BC', 'SO', 'PR', 'HK', 'XI', 'RN', 'MK', 'ZY', 'FG', 'GX', 'DX', 'EM',
    'FA', 'IS', 'TI', 'TU', 'PI', 'UP', 'LP', 'IR', 'EI', 'VB', 'TH', 'FU', 'TC', 'HU', 'BU', 'NU',
    'LR', 'SS', 'IJ', 'LA', 'PN', 'TW', 'BS', 'KG', 'CV', 'SF', 'ER', 'MM', 'AZ', 'MV', 'OD', 'LE',
    'MS', 'JE', 'TK', 'MQ', 'CA', 'EW', 'VO', 'XA', 'CM', 'TN', 'UN', 'RR', 'UL', 'RY', 'YW', 'FX',
    'OK', 'GW', 'HI', 'NM', 'EZ', 'SI', 'IZ', 'SA', 'DR', 'QA', 'JO', 'HN', 'QN', 'HZ', 'MX', 'PK',
    'FI', 'DC', 'RD', 'QS', 'WL', 'AJ', 'VP', 'UV', 'BB', 'AY', 'YN', 'NK', 'UR', 'OM', 'DG', 'XD',
    'NY', 'XM', 'DT', 'KF', 'JC', 'KR', 'TL', 'RI', 'YM', 'EN', 'OU', 'RS', 'DB', 'LD', 'NG', 'AM',
    'JT', 'NB', 'BJ', 'OL', 'UT', 'HD', 'WA', 'IE', 'II', 'KS', 'YP', 'VE', 'BR', 'RW', 'AU', 'SQ',
    'JV', 'HW', 'CS', 'IL', 'UU', 'DJ', 'SW', 'AQ', 'JM', 'TE', 'EO', 'LS', 'SB', 'CU', 'QR', 'SH',
    'WN', 'DH', 'ZN', 'LN', 'AW', 'SL', 'DI', 'KY', 'EF', 'ES', 'KW', 'XC', 'OV', 'CK', 'DD', 'IN',
    'WS', 'KE', 'PP', 'RU', 'UH', 'EH', 'RE', 'MC', 'ZA', 'SN', 'WW', 'HY', 'OW', 'BE', 'OG', 'AH',
    'BN', 'QE', 'NP', 'RC', 'ZZ', 'QM', 'BX', 'CT', 'WR', 'SG', 'TR', 'BI', 'MB', 'ED', 'RX', 'SJ',
    'NA', 'OC', 'DP', 'NO', 'JD', 'FO', 'FL', 'HH', 'FW', 'TT', 'QT', 'NZ', 'CR', 'AG', 'HL', 'TF',
    'VN', 'SK', 'YO', 'LV', 'GI', 'VI', 'SR', 'YT', 'CB', 'HE', 'KP', 'XT', 'PG', 'RG', 'JR', 'TP',
    'HM', 'AR', 'MW', 'BG', 'HS', 'AP', 'GF', 'KM', 'NJ', 'RK', 'YE', 'NX', 'AN', 'XF', 'AX', 'LM',
    'PM', 'GB', 'DS', 'IG', 'UX', 'GL', 'GS', 'PT', 'VD', 'FF', 'OO', 'GD', 'XY', 'IC', 'UZ', 'KU',
)

__all__ = ["DEFAULT_COMPACT_BOUNDARY_CODEBOOK"]
