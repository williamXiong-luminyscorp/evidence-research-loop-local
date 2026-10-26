# Capacity-buffer strategy task

Edit `candidate.json` while preserving exactly these numeric fields:

- `safety_capacity`: reserve capacity used to protect service, integer from 0 through 40.
- `batch_size`: replenishment batch size, integer from 1 through 60.
- `review_period`: review interval, integer from 1 through 7.

The strategy is evaluated on multiple demand-pressure scenarios. A service level of at least 0.95 in every recorded aggregate is a hard gate. Among feasible strategies, minimize total operating cost and average committed capacity. The evaluator and holdout scenarios are not available to the candidate generator.
