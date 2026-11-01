import type { ReactNode } from "react";
export default function RepeatHighlight({text,offset,spans}:{text:string;offset:number;spans:{start:number;end:number}[]}) {
  const chars=Array.from(text),ranges: [number,number][]=[];
  for(const span of [...spans].sort((a,b)=>a.start-b.start)) {
    const start=Math.max(0,span.start-offset),end=Math.min(chars.length,span.end-offset);
    if(start>=end)continue;
    if(ranges.length&&start<=ranges[ranges.length-1][1])ranges[ranges.length-1][1]=Math.max(end,ranges[ranges.length-1][1]);
    else ranges.push([start,end]);
  }
  const parts:ReactNode[]=[];let cursor=0;
  for(const [start,end] of ranges){parts.push(chars.slice(cursor,start).join(""));parts.push(<mark key={start}>{chars.slice(start,end).join("")}</mark>);cursor=end;}
  parts.push(chars.slice(cursor).join(""));
  return <>{parts}</>;
}
