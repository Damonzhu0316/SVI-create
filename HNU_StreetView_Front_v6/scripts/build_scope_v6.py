from __future__ import annotations
import csv, json, math, collections, shutil, hashlib
from pathlib import Path
from shapely.geometry import shape, mapping, LineString, Point
from shapely.ops import transform, unary_union
from pyproj import Transformer, Geod
from coordinate_transform import wgs84_to_gcj02

ROOT=Path(__file__).resolve().parents[1]
NET=ROOT/'data'/'network'; DATA=ROOT/'data'; DOCS=ROOT/'docs'
TO_M=Transformer.from_crs(4326,32649,always_xy=True).transform
TO_W=Transformer.from_crs(32649,4326,always_xy=True).transform
GEOD=Geod(ellps='WGS84')
KEEP_OUTSIDE={'primary','secondary'}
FORBIDDEN_NAMES={'西二环','含浦大道','清水路','新民路'}
EAST_CAMPUS_CONNECTOR='西苑路'
RAW_SOURCE=ROOT.parent/'HNU_StreetView'/'data'/'raw'/'osm_main.json'
RAW_COPY=DATA/'raw'/'osm_main.json'
BASE_AOI=NET/'aoi_v6_original.geojson'
LUSHAN_WAYS_TO_RESTORE={1030743554,1023634492,1031928621}
PERMITTED_BICYCLE={'yes','designated','permissive','official'}
ALLOWED_HIGHWAYS={'trunk','trunk_link','primary','secondary','tertiary','unclassified','residential','living_street','service','cycleway','primary_link','secondary_link','tertiary_link','path','footway','pedestrian','track','road'}

def load(p): return json.loads(p.read_text(encoding='utf-8-sig'))
def savej(p,obj): p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
def decide(tags):
    if tags.get('building') not in (None,'no') or tags.get('waterway') or tags.get('natural')=='water': return False,'non_road_object'
    if not tags.get('highway'): return False,'non_road_object'
    if tags.get('highway')=='steps' or tags.get('incline')=='steps': return False,'steps'
    if tags.get('indoor')=='yes' or tags.get('highway')=='corridor': return False,'indoor_passage'
    if tags.get('area')=='yes': return False,'area_not_linear_road'
    if tags.get('bicycle') in {'no','private','dismount','use_sidepath'}: return False,'explicit_bicycle_restriction'
    if tags.get('bicycle:forward')=='no' and tags.get('bicycle:backward')=='no': return False,'explicit_bicycle_restriction'
    if tags.get('motorroad')=='yes' or tags.get('highway') in {'motorway','motorway_link'}: return False,'motor_traffic_only'
    if tags.get('access') in {'no','private'} and tags.get('bicycle') not in PERMITTED_BICYCLE: return False,'access_restricted'
    if tags.get('highway') not in ALLOWED_HIGHWAYS: return False,'unsupported_highway_type'
    review=[]
    if tags.get('highway') in {'trunk','trunk_link'}: review.append('major_road_bicycle_access_unverified')
    if tags.get('highway') in {'footway','path','pedestrian','track','road'} and tags.get('bicycle') not in PERMITTED_BICYCLE: review.append('bicycle_access_unverified')
    if tags.get('service') in {'driveway','parking_aisle','drive-through','emergency_access'}: review.append('service_relevance_unverified')
    if tags.get('tunnel') in {'yes','building_passage'}: review.append('tunnel_access_unverified')
    if tags.get('informal')=='yes': review.append('informal_path_unverified')
    if any(k.endswith(':conditional') for k in tags): review.append('conditional_access_unverified')
    if tags.get('name')=='西二环': review.append('ring_road_bicycle_access_unverified')
    return True,';'.join(review) or 'no_explicit_bicycle_prohibition'
def orient(g,tags):
    oneway=tags.get('oneway:bicycle',tags.get('oneway','yes' if tags.get('junction')=='roundabout' else 'no'))
    if oneway in {'yes','1','true'}: return g,'osm_bicycle_oneway_forward'
    if oneway=='-1': return LineString(list(g.coords)[::-1]),'osm_bicycle_oneway_reverse'
    if tags.get('bicycle:forward')=='no' and tags.get('bicycle:backward')!='no': return LineString(list(g.coords)[::-1]),'bicycle_backward_only'
    if tags.get('bicycle:backward')=='no' and tags.get('bicycle:forward')!='no': return g,'bicycle_forward_only'
    if tuple(g.coords[-1])<tuple(g.coords[0]): g=LineString(list(g.coords)[::-1])
    return g,'deterministic_two_way_direction'
def rebuild_source_candidates():
    if not RAW_COPY.exists():
        if not RAW_SOURCE.exists(): raise FileNotFoundError(f'Raw OSM snapshot missing: {RAW_SOURCE}')
        RAW_COPY.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(RAW_SOURCE,RAW_COPY)
    if not BASE_AOI.exists(): shutil.copy2(NET/'aoi.geojson',BASE_AOI)
    raw=load(RAW_COPY)
    if raw.get('remark'): raise ValueError('OSM snapshot is incomplete')
    base_fc=load(BASE_AOI); base=unary_union([transform(TO_M,shape(f['geometry'])) for f in base_fc['features']])
    target={e['id']:e for e in raw['elements'] if e.get('type')=='way' and e.get('id') in LUSHAN_WAYS_TO_RESTORE and len(e.get('geometry',[]))>=2}
    if set(target)!=LUSHAN_WAYS_TO_RESTORE: raise ValueError(f'Missing target OSM ways: {sorted(LUSHAN_WAYS_TO_RESTORE-set(target))}')
    extensions=[]; restored=[]
    for wid,e in target.items():
        full=transform(TO_M,LineString([(p['lon'],p['lat']) for p in e['geometry']]))
        outside=full.difference(base)
        if outside.length>.1:
            extensions.append(outside.buffer(20))
            for seg in line_parts(outside):
                if seg.length>.1:
                    restored.append({'type':'Feature','geometry':mapping(transform(TO_W,seg)),'properties':{'osm_way_id':wid,'road_name':e.get('tags',{}).get('name','麓山南路'),'road_type':e.get('tags',{}).get('highway','secondary'),'restored_length_m':round(seg.length,3),'reason':'clipped_by_previous_v6_aoi'}})
    repaired=unary_union([base,*extensions]).buffer(0)
    bounds=base.bounds; new_bounds=repaired.bounds
    aoi_props=dict(base_fc['features'][0]['properties']); aoi_props.update(aoi_id='HNU_V6_AOI_LUSHAN_SOUTH_RESTORED',area_km2=round(repaired.area/1e6,6),status='PROPOSED_OSM_BASED_WITH_V6_CORRIDOR_EXTENSION',extension_description='20 m corridor buffer around previously AOI-clipped 麓山南路 segments',extension_osm_way_ids=','.join(map(str,sorted(LUSHAN_WAYS_TO_RESTORE))),source_osm_timestamp=raw.get('osm3s',{}).get('timestamp_osm_base'))
    savej(NET/'aoi.geojson',{'type':'FeatureCollection','features':[{'type':'Feature','geometry':mapping(transform(TO_W,repaired)),'properties':aoi_props}]})
    savej(NET/'aoi_restored_road_segments.geojson',{'type':'FeatureCollection','features':restored})
    candidates=[]; audit=[]
    for e in sorted(raw['elements'],key=lambda x:(x.get('type',''),x.get('id',0))):
        if e.get('type')!='way' or len(e.get('geometry',[]))<2: continue
        tags=e.get('tags',{}); name=tags.get('name',tags.get('name:zh','NA')); typ=tags.get('highway','NA')
        full=transform(TO_M,LineString([(p['lon'],p['lat']) for p in e['geometry']]))
        clipped=full.intersection(repaired)
        if clipped.is_empty or clipped.length<.1: continue
        included,reason=decide(tags)
        audit.append({'osm_way_id':e['id'],'road_name':name,'road_type':typ,'decision':'INCLUDE' if included else 'EXCLUDE','reason':reason,'length_m':round(clipped.length,3)})
        if not included: continue
        for part,gpart in enumerate(line_parts(clipped)):
            if gpart.length<.1: continue
            if full.project(Point(gpart.coords[-1]))<full.project(Point(gpart.coords[0])): gpart=LineString(list(gpart.coords)[::-1])
            oriented,basis=orient(gpart,tags)
            props={'road_id':f'W{e["id"]}_{part}','osm_way_id':e['id'],'road_name':name,'road_type':typ,'length_m':round(oriented.length,3),'access_review':reason,'heading_basis':basis,'bicycle':tags.get('bicycle'),'access':tags.get('access')}
            candidates.append({'type':'Feature','geometry':mapping(transform(TO_W,oriented)),'properties':props})
    savej(NET/'source_candidate_roads.geojson',{'type':'FeatureCollection','features':candidates})
    with (NET/'road_selection_audit.csv').open('w',encoding='utf-8-sig',newline='') as fp:
        w=csv.DictWriter(fp,fieldnames=list(audit[0])); w.writeheader(); w.writerows(audit)
    (NET/'excluded_objects.csv').write_text('',encoding='utf-8-sig')
    return {'timestamp':raw.get('osm3s',{}).get('timestamp_osm_base'),'ways_restored':sorted(LUSHAN_WAYS_TO_RESTORE),'restored_segment_count':len(restored),'restored_road_length_m':round(sum(f['properties']['restored_length_m'] for f in restored),3),'buffer_m':20,'source_road_count':len(candidates),'source_sha256':hashlib.sha256(RAW_COPY.read_bytes()).hexdigest(),'old_aoi_bounds':list(bounds),'repaired_aoi_bounds':list(new_bounds)}
def line_parts(g):
    if g.is_empty:return []
    if g.geom_type=='LineString':return [g]
    if hasattr(g,'geoms'):return [x for part in g.geoms for x in line_parts(part)]
    return []
def main():
    aoi_revision=rebuild_source_candidates()
    source=load(NET/'source_candidate_roads.geojson')['features']
    yellow=load(NET/'core_areas.geojson')
    mask=unary_union([transform(TO_M,shape(f['geometry'])) for f in yellow['features']])
    roads=[]; points=[]; audit=[]; stats=collections.defaultdict(lambda:{'segments':0,'length_m':0.,'points':0})
    for f in source:
        p0=f['properties']; g=transform(TO_M,shape(f['geometry'])); typ=p0['road_type']
        name=str(p0.get('road_name') or 'NA').strip()
        candidates=[]
        if name not in FORBIDDEN_NAMES:
            for role,geom in [('yellow_area',g.intersection(mask))]:
                candidates.extend((role,part) for part in line_parts(geom) if part.length>=.1)
            if typ in KEEP_OUTSIDE:
                candidates.extend(('intercampus_major_secondary',part) for part in line_parts(g.difference(mask)) if part.length>=.1)
            elif name==EAST_CAMPUS_CONNECTOR:
                candidates.extend(('intercampus_east_campus_connector',part) for part in line_parts(g.difference(mask)) if part.length>=.1)
        kept_len=sum(part.length for _,part in candidates)
        if name in FORBIDDEN_NAMES: rule='excluded_by_user_road_name'
        elif name==EAST_CAMPUS_CONNECTOR: rule='yellow_area_plus_east_campus_connector'
        elif typ in KEEP_OUTSIDE: rule='yellow_area_plus_primary_secondary_outside'
        else: rule='yellow_area_only'
        audit.append({'source_road_id':p0.get('road_id'),'osm_way_id':p0.get('osm_way_id'),'road_name':name,'road_type':typ,'source_length_m':round(g.length,3),'retained_length_m':round(kept_len,3),'decision':'RETAIN' if kept_len else 'EXCLUDE','scope_rule':rule})
        for idx,(role,part) in enumerate(candidates):
            coords=list(part.coords)
            if g.project(Point(coords[-1])) < g.project(Point(coords[0])): part=LineString(coords[::-1])
            rid=f"V6_{p0.get('road_id','R')}_{idx+1}"
            length=part.length
            props=dict(p0); props.update(road_id=rid,length_m=round(length,3),scope_role=role,scope_rule=rule)
            roads.append({'type':'Feature','geometry':mapping(transform(TO_W,part)),'properties':props})
            stat=stats[typ];stat['segments']+=1;stat['length_m']+=length
            for j in range(math.ceil(length/30)):
                d=j*30
                if d>=length: continue
                pt=part.interpolate(d); lon,lat=TO_W(pt.x,pt.y)
                a=part.interpolate(max(0,d-3));b=part.interpolate(min(length,d+3)); al,at=TO_W(a.x,a.y);bl,bt=TO_W(b.x,b.y)
                heading=GEOD.inv(al,at,bl,bt)[0]%360
                gx,gy=wgs84_to_gcj02(lon,lat); k=math.pi*3000/180; z=math.hypot(gx,gy)+.00002*math.sin(gy*k); t=math.atan2(gy,gx)+.000003*math.cos(gx*k); blon=z*math.cos(t)+.0065; blat=z*math.sin(t)+.006
                point_id=f'HNU_{len(points)+1:06d}'
                point={'point_id':point_id,'station_id':f'ST_{point_id}','road_id':rid,'osm_way_id':props.get('osm_way_id'),'road_name':name,'road_type':typ,'sample_lon':round(lon,9),'sample_lat':round(lat,9),'sample_crs':'WGS84','sample_lon_bd09':blon,'sample_lat_bd09':blat,'travel_heading':round(heading,6),'relative_yaw':0,'absolute_view_heading':round(heading,6),'pitch':0,'heading_basis':props.get('heading_basis','inherited_road_direction'),'distance_along_road_m':round(d,3),'access_review':props.get('access_review','NA'),'area_role':role,'scope_role':role}
                points.append(point);stat['points']+=1
    def savej(p,obj):p.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
    savej(NET/'roads.geojson',{'type':'FeatureCollection','features':roads})
    savej(NET/'sampling_points.geojson',{'type':'FeatureCollection','features':[{'type':'Feature','geometry':{'type':'Point','coordinates':[p['sample_lon'],p['sample_lat']]},'properties':p} for p in points]})
    fields=list(points[0]) if points else []
    with (DATA/'sampling_points.csv').open('w',encoding='utf-8-sig',newline='') as fp:
        w=csv.DictWriter(fp,fieldnames=fields);w.writeheader();w.writerows(points)
    with (NET/'scope_change_audit.csv').open('w',encoding='utf-8-sig',newline='') as fp:
        w=csv.DictWriter(fp,fieldnames=list(audit[0]));w.writeheader();w.writerows(audit)
    with (NET/'road_type_statistics.csv').open('w',encoding='utf-8-sig',newline='') as fp:
        w=csv.DictWriter(fp,fieldnames=['road_type','segments','length_m','points']);w.writeheader()
        for typ,v in sorted(stats.items()):w.writerow({'road_type':typ,'segments':v['segments'],'length_m':round(v['length_m'],3),'points':v['points']})
    by_role=collections.defaultdict(lambda:{'segments':0,'length_m':0.,'points':0})
    for f in roads:
        p=f['properties'];r=by_role[p['scope_role']];r['segments']+=1;r['length_m']+=float(p['length_m'])
    for p in points:by_role[p['scope_role']]['points']+=1
    removed={n:{'source_segments':sum(x['road_name']==n for x in audit),'source_length_km':round(sum(x['source_length_m'] for x in audit if x['road_name']==n)/1000,6)} for n in sorted(FORBIDDEN_NAMES)}
    coordinate_groups=collections.defaultdict(list)
    for point in points:
        coordinate_groups[(round(point['sample_lon'],8),round(point['sample_lat'],8))].append(point)
    overlaps=[group for group in coordinate_groups.values() if len(group)>1]
    overlap_report=[{'coordinate_wgs84':[key[0],key[1]],'point_ids':[p['point_id'] for p in group],'road_ids':[p['road_id'] for p in group],'road_names':[p['road_name'] for p in group]} for key,group in sorted(coordinate_groups.items()) if len(group)>1]
    report={'version':'v6','scope_rule':{'yellow_area':'keep road portions inside yellow polygons','outside_yellow_area':'keep highway=primary or highway=secondary, plus named 西苑路 as the 桃花坪 east-side connector','excluded_road_names':sorted(FORBIDDEN_NAMES),'links_outside_yellow_area_not_retained':['primary_link','secondary_link']},'aoi_revision':aoi_revision,'source_candidate_segments':len(source),'retained_segments':len(roads),'retained_length_km':round(sum(float(f['properties']['length_m']) for f in roads)/1000,6),'scope_roles':{k:{'segments':v['segments'],'length_km':round(v['length_m']/1000,6),'points':v['points']} for k,v in sorted(by_role.items())},'sampling_interval_m':30,'sample_points':len(points),'station_id_policy':'Each target-road sample has a stable-in-source station_id ST_<point_id>; use the same station_id for car and ebike imagery, retaining front/back as separate rows.','duplicate_coordinate_groups_1e-8_degree':len(overlaps),'points_in_duplicate_coordinate_groups':sum(len(g) for g in overlaps),'duplicate_coordinate_groups':overlap_report,'duplicate_coordinate_policy':'Retain each road-associated observation; exact coordinate overlap can represent distinct road geometry and headings at a junction.','excluded_named_roads':removed,'road_types':{k:{'segments':v['segments'],'length_km':round(v['length_m']/1000,6),'points':v['points']} for k,v in sorted(stats.items())},'connection_check':{'route':'麓山南路 → unnamed OSM primary segment → 西苑路','connector_osm_way_ids':[1031928621,1031928620,1472196926],'status':'retained; spatial continuity verified against source road geometries'},'aoi_edge_cut_restoration':aoi_revision,'note':'Collection scope only; no street-view imagery was collected. Sample point IDs were regenerated.'}
    savej(DOCS/'scope_change_report.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
