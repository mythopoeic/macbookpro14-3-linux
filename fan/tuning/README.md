
| `load-allcore-pl1-verified.log` | All-core load with PL1 sampled every 2 s alongside temperature. The run that corrected the earlier Tjmax conclusion: PL1 held 33-42 W, package peaked at 96 C, never reaching Tjmax. |
| `rapl-cap-holds-at-idle.log` | PL1 watched for 36 s with thermald running and again with it stopped. Held at 40 W in both, which is how the revert was narrowed to thermal stress rather than a continuous second writer. |
