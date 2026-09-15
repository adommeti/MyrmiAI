# Weekly digest - Sep 7-13, 2026

*5 items across 2 categories. Generated 2026-09-14 11:00 UTC.*

**In this issue:** AI Engineering & Agents (2), Cybersecurity (3)

> 1 item(s) had no transcript or article text and were summarised from titles and descriptions only. These are marked *metadata only*.

## AI Engineering & Agents

A quieter week with one substantive argument: that the practical constraint on agent quality has moved from prompt wording to what you put in the context window and when. The saved Stratechery piece arrives at a similar place from the market side, treating MCP as a control plane rather than a file format.

**Themes**
- Context assembly, not prompt phrasing, framed as the current bottleneck (Latent Space, stratechery.com)

**Start here**
- [Context assembly, not prompting, now bounds agent quality](https://www.youtube.com/watch?v=DDD) *(Latent Space)* - The retrieval-window benchmark in the back half is the part worth your time.
- [MCP is becoming a control plane, not just a protocol](https://stratechery.com/2026/agents/) *(stratechery.com)* - Matches your note — the control-plane framing is the load-bearing argument.

<details><summary>All items</summary>

### [Context assembly, not prompting, now bounds agent quality](https://www.youtube.com/watch?v=DDD)
*Latent Space · 1h18m · signal 5/5*

- Argues eval gains now come from retrieval and context ordering, not prompt rewrites
- Benchmarks a 1M-token window against selective retrieval: selective wins on both cost and accuracy above ~200k tokens
- Recommends treating context assembly as a versioned artifact with its own tests

**Why it matters:** Reframes where to spend engineering effort on internal agents, and argues against the reflex to enlarge the context window.

### [MCP is becoming a control plane, not just a protocol](https://stratechery.com/2026/agents/)
*stratechery.com · signal 4/5*

- Frames MCP adoption as a distribution play rather than a technical standard
- Argues the governance surface (who can connect what) is where enterprise value accrues

**Why it matters:** Directly relevant to how agent connectors get reviewed and approved internally.

</details>

## Cybersecurity

Two of the three security items this week converge on the same shift: detection content is moving from vendor-maintained IOC lists to portable, version-controlled rules. Microsoft's release notes and Black Hills' walkthrough describe the same problem from opposite ends — one shipping the capability, the other explaining why teams keep misconfiguring it.

**Themes**
- Detection-as-code keeps consolidating on Sigma (Black Hills InfoSec, Microsoft Security)
- Both sources flag false-positive tuning, not rule authoring, as the real bottleneck

**Start here**
- [Sigma rules cut lateral-movement false positives by ~60%](https://www.youtube.com/watch?v=AAA) *(Black Hills Information Security)* - Walks through the actual rule diffs; the tuning method transfers directly to Sentinel.

<details><summary>All items</summary>

### [Sigma rules cut lateral-movement false positives by ~60%](https://www.youtube.com/watch?v=AAA)
*Black Hills Information Security · 1h02m · signal 5/5*

- Reports a drop from ~40 to ~15 daily alerts after tuning three Sigma rules against Entra ID sign-in logs
- Recommends scoping by service-principal ID rather than IP, which is what caused most of their noise
- Ships the rule set at github.com/…/sigma-lateral; MIT licensed

**Why it matters:** The scoping change is directly portable to Sentinel analytics rules and is the kind of fix that is cheap to trial.

### [Defender XDR adds custom detection rules scoped to workload identities](https://www.youtube.com/watch?v=BBB)
*Microsoft Security · 24m · signal 4/5*

- Custom detections can now target workload identities, previously user-principals only
- Attack-disruption coverage extends to SharePoint; no change to licensing tier

**Why it matters:** Closes a real gap if you run service-principal-heavy automation, which most Copilot and agent deployments do.

</details>

**Skipped (1):** [Weekly infosec news roundup](https://www.youtube.com/watch?v=CCC)

---

<sub>Add links for next week's digest to `inbox/links.md`. Adjust categories in `config/taxonomy.yaml`.</sub>
