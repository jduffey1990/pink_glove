/**
 * Only the newest answer counts.
 *
 * A page that reloads when a picker changes can have two requests in the
 * air, and nothing says they land in the order they were sent. If last
 * week's figures arrive after this week's, the page shows last week's hours
 * under a heading that says this week: wrong numbers, with no error.
 *
 * Call the tracker when a load starts; it hands back a check that is true
 * only while no later load has started.
 *
 *   const begin = latestOnly()
 *   async function load () {
 *     const isCurrent = begin()
 *     const data = await fetchIt()
 *     if (isCurrent()) { show(data) }
 *   }
 */
export function latestOnly (): () => () => boolean {
  let newest = 0

  return () => {
    const mine = ++newest
    return () => mine === newest
  }
}
