# Activity explorer

Implement a usable activity explorer for a developer tracking work across projects.
The supplied app is a minimal static starter. Edit files under `web/`; all files
there are submitted. Use HTML, CSS and browser JavaScript. Visual design is yours:
make the interface coherent, readable and efficient to scan. No external downloads
or services are available. A local browser, fonts, Node and pnpm are installed.

Run `pnpm dev` for the supplied local server, `pnpm build` to check static assets,
and use Playwright to inspect screenshots and interactions. Changes outside `web/`
are development scratch, not part of the submitted app. The evaluator serves
your static assets independently, without running your scripts or package config.

## Data and interactions

Fetch `/api/activities` on load. The response is `{ "activities": [...] }` with
records containing unique `id`, `title`, `project`, `status` (`running`, `completed`,
or `failed`), and multiline `description` strings. Preserve response order.

- Provide a labeled search input named **Search activities**, and a select named
  **Status**, with options **All**, **Running**, **Completed**, **Failed**.
- Search is case-insensitive substring matching over title and project. Combine
  search and status filters with AND; changing either updates the visible list.
- Each matching record has one button with its full title as accessible name.
  Clicking it shows the full title, project, status and complete description in
  a labeled region named **Activity details**. Include a **Close details** button.
- Show **No activities found** when the filtered result is empty. Do not render
  stale matching buttons after changing filters.
- While a request is pending, show **Loading activities**. On a failed request,
  show **Could not load activities** and a **Retry** button that fetches again.
  After successful retry, clear the error. On empty data, show the empty state.
- Search, status, record buttons and close/retry controls must work with keyboard
  navigation; use native controls and a clearly visible keyboard focus style.

## Layout and content

Support viewports from 360 to 1440 CSS pixels wide, with heights of at least 640.
Content can include up to 30 records, titles of 160 characters, project names of
80 characters, and descriptions of 2000 characters. Long strings can contain
unbroken tokens. Full titles must remain readable in the list and detail view;
wrapping is appropriate. Do not hide required text to avoid overflow.
Use at least 14px text and 24px-high activity controls at the default text size.

No document-level horizontal scrolling, clipped required text, overlapping
interactive controls, or controls unreachable by ordinary scrolling. Use document
scrolling for the results list; do not put it in a separate scrolling pane. A
detail overlay may scroll vertically when needed, but must fit horizontally and
allow its close action to remain reachable. Preserve existing content when
opening/closing details. The interface must remain usable with text enlarged to 150% of its default size.

## Verification and evaluation

Exercise search, combined filters, details, loading, empty and error/retry states
at narrow and wide widths. Inspect rendered screenshots and check long content.
Acceptance checks use additional data within the declared bounds and semantic
labels above, not a prescribed DOM structure or pixel-perfect reference image.
Functional/layout checks and human design preference are separate outcomes.
