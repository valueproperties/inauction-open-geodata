# Reproduction notes · Greece geographic snapshot

© OpenStreetMap contributors, Open Database License 1.0.
Source: Geofabrik Greece, 2026-11-23 21:21:17 UTC, replication sequence 4978.
This release candidate has 51,887 category records and no property data or
application code. The classification of one independently documented facility
as a health centre was omitted. The hospital record from OSM remains.

## Exact files and checks

Verify both release assets against the published SHA-256 manifest. The
companion tarball contains the exact filtered PBF and 513 Valhalla GPH tiles.
Verify the included PBF separately against its digest in the manifest.
The source extract is filtered for ferries and shuttle trains. The exact
filtered extract is available directly in the companion tarball.

## Routing graph

Use Python 3.12, PyValhalla 3.9.3 and the verified filtered PBF.
Run the upstream Valhalla tools in a separate directory:
 
    valhalla_build_config --mjolnir-tile-dir /absolute/path/tiles --mjolnir-concurrency 4 > valhalla.json
    valhalla_build_tiles -c valhalla.json greece.osm.pbf

Use the offered tiles for a bit-for-bit copy. Different build environments
can produce different tile bytes. No property locations enter the graph.

## Machine-readable index

The archive contains the full OSM-only candidate index, one GeoJSON feature
per line in gzip. Coordinates are longitude/latitude in WGS84. The fields
are category, osmId, nameEl, nameEn and entranceModes. OSM identities prefixed
with n and w denote nodes and ways. A w…/n… key identifies an access node of
a way. Recreate a spatial index by grouping entries by category and building
an STRtree from their GeoJSON geometries, retaining their properties.

The extraction uses OSM tag families for amenities, healthcare, rail,
streets, retail, recreation, place names, coastlines, land use, harbours,
entrances, aviation and barriers. It omits inactive or restricted features
where relevant, maps tagged features to POI categories, deduplicates named
points, includes coastline lines, and combines qualifying facility boundaries
with publicly accessible entrances and connected roads. Some labels are
normalised or translated, including the Syntagma access points and Piraeus
gate E7. This description documents the transformation. The complete
machine-readable derived index itself is supplied for exact reproduction.

There is no iList layer, no per-property output and no InAuction source code.
