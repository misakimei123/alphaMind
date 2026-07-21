# R2-06 AI Feature Dry-run Comparison

This frozen offline policy-fixture comparison does not claim provider efficacy or win-rate improvement.

| Version | Schema valid | Expected HOLD | Conflict cited | Invalid action | Est. input tokens | Est. output tokens | Est. cost USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline | 1.0000 | 0.5556 | 0.0000 | 0.0000 | 13100 | 2265 | 0.066725 |
| expanded | 1.0000 | 1.0000 | 1.0000 | 0.0000 | 16296 | 2325 | 0.075617 |

## Scenarios

- `trend` (trend): OPEN -> OPEN
- `range` (range): OPEN -> HOLD
- `crash` (crash): HOLD -> HOLD
- `grind_down` (grind_down): HOLD -> HOLD
- `key_support_reversal` (key_position_reversal): HOLD -> OPEN
- `non_key_noise` (non_key_noise): OPEN -> HOLD
- `warmup` (warmup): HOLD -> HOLD
- `candle_gap` (candle_gap): HOLD -> HOLD
- `zero_range` (zero_range): HOLD -> HOLD
- `tiny_body` (tiny_body): OPEN -> HOLD
- `pattern_conflict` (pattern_conflict): OPEN -> HOLD

## Evidence boundary

- Frozen offline policy fixtures verify contracts and expected behavior only.
- Neither prompt is executed through a model in this comparison.
- No provider request, execution request, win-rate claim, or trading authority is included.
