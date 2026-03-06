# WikiGame

[![OpenReward Environment](https://img.shields.io/badge/%E2%AD%90%20OpenReward-Environment-f7e6cc)](https://openreward.ai/GeneralReasoning/WikiGame)

## Description

**WikiGame** is an environment that implements the Wikipedia Game, where agents navigate from a start Wikipedia article to a target article by clicking hyperlinks on each page. The agent must find a path between two articles using only the links available on each page. The environment fetches live Wikipedia content via the Wikipedia API and presents articles as paginated markdown with inline links.

## Capabilities

- Navigating between Wikipedia articles via hyperlinks
- Strategic path planning across the Wikipedia link graph
- Reading comprehension of article content to identify relevant links
- Multi-turn decision-making with a limited click budget

## Compute Requirements

WikiGame does not require a sandbox. It has minimal compute requirements but requires network access to the Wikipedia API at runtime.

## Tasks

There are two splits: train (1,000 tasks) and test (100 tasks). Tasks are loaded from a `tasks.json` file. Each task specifies a start article and a target article. The agent must navigate from the start to the target by clicking links on each page, within a maximum of 20 clicks.

## Reward Structure

This is a dense, verifiable reward environment with negative scoring. The reward is based on the negative number of clicks used to reach the target:

$$\text{Reward} = \frac{20-\text{clicks}}{20} $$

Note if the agent uses all 20 clicks, they receive a reward of $0$. Invalid clicks have no penalty (the click is not counted) and the agent may retry.

We do not use LLM graders for this task.

## Data

Tasks are stored in a `tasks.json` file on the OpenReward platform. Article content is fetched live from the Wikipedia API at runtime. Articles are converted from HTML to markdown with inline wiki-links, and paginated into ~8,000 character pages. Sections like "See also", "References", and "External links" are stripped.

## Tools

Agents are given two tools:

- `click_link`: Navigate to a linked article by specifying its title. Costs 1 click. Returns the new article's content (page 1) with current click count, or an error with suggestions if the link is not found on the current page.
- `view_page`: View a specific page of the current article (free, no click cost). Used for reading longer articles that span multiple pages.

## Time Horizon

WikiGame is a multi-turn environment. Each task may require up to 20 clicks (plus free `view_page` calls) to navigate from start to target. The number of tool calls varies depending on the article distance and agent strategy.

[Statistics on average tool calls here]

## Environment Difficulty

[Statistics on environment difficulty here]

## Other Environment Requirements

WikiGame requires network access to the Wikipedia API (`https://en.wikipedia.org/w/api.php`) at runtime for fetching article content. The environment includes cached articles (based on past agent runs) to reduce network latency and API costs.

## Safety

Agents in WikiGame navigate Wikipedia articles by clicking links. The environment does not present direct safety risks, as agents only interact with public Wikipedia content in a read-only fashion. The agent cannot edit Wikipedia or access any external systems beyond the Wikipedia API.

## Citations

```bibtex
@dataset{GRWikiGame,
  author    = {General Reasoning Inc. Team},
  title     = {WikiGame},
  year      = {2026},
  publisher = {OpenReward},
  url       = {https://openreward.ai/GeneralReasoning/WikiGame}
}
```
