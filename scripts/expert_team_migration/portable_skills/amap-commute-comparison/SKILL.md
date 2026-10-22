---
name: amap-commute-comparison
description: Compare real commute routes between candidate homes and a workplace through an installed AMap MCP connection. Use for rental-area comparisons, transit or driving commute estimates, route distance, transfers, and time-based location decisions; do not use a map-development tutorial or model guesses as route evidence.
---

# AMap Commute Comparison

Use the installed AMap MCP tools as the route-data authority.

1. Keep the user's origin, destination, city, transport modes, departure-time
   assumptions, and maximum acceptable commute as explicit inputs.
2. Use `tool_search` to discover the connected AMap geocoding and route-planning
   tools. Do not assume a provider-specific tool name that the runtime did not return.
3. Geocode every candidate and destination before route comparison. Preserve the
   resolved address and coordinates so ambiguous places remain visible.
4. Query each requested mode with the same assumptions. Record duration, distance,
   transfers or tolls when supplied, query time, and provider.
5. Rank candidates only after separating verified route results from rent, amenity,
   or user-preference assumptions.

If the AMap MCP connection, required tool, credential, or route result is unavailable,
mark that field `待核实` and tell the leader which capability is missing. Do not replace
route data with JavaScript API documentation, search snippets, straight-line distance,
or an invented commute time. Never put an AMap credential in the package or output.
