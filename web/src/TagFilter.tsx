import { useLoad, count } from "./shared";
import type { Facet, Filter } from "./datasetTypes";

type Vocabulary = { version: string; dimensions: Record<string, { id: string; name: string }[]> };
const dimensions: Record<string, string> = {
  era: "时代", genre: "体裁", class: "部类", region: "地域", jurisdiction: "发布法域",
};

/** 规范标签选择：树形勾选，选中某级即含全部下级；“未标注”选没有该维度标签的文档。 */
export function TagFilter({ filter, onChange, counts }: {
  filter: Filter; onChange: (f: Filter) => void; counts?: Record<string, Facet[]>;
}) {
  const vocabulary = useLoad<Vocabulary>("taxonomy");
  if (!vocabulary.data) return null;
  const tags = (filter.tags as Record<string, string[]> | undefined) || {},
    untagged = (filter.untagged as string[] | undefined) || [];

  function update(dim: string, ids: string[], none: boolean) {
    const nextTags = { ...tags, [dim]: ids }, nextUntagged = untagged.filter(d => d !== dim).concat(none ? [dim] : []);
    if (!ids.length) delete nextTags[dim];
    const next: Filter = { ...filter, tags: nextTags, untagged: nextUntagged };
    if (!Object.keys(nextTags).length) delete next.tags;
    if (!nextUntagged.length) delete next.untagged;
    onChange(next);
  }

  return <div className="tag-filter">
    <h4>规范标签（多选，选上级含下级）</h4>
    {Object.entries(dimensions).map(([dim, label]) => {
      const items = vocabulary.data!.dimensions[dim] || [], selected = tags[dim] || [], none = untagged.includes(dim);
      const own: Record<string, Facet> = {};
      for (const f of counts?.[dim] || []) own[f.value ?? ""] = f;
      // 计数按标签：一篇文档在同一上级下有两个标签时，上级合计会重复计入。
      const total = (id: string) => items.filter(x => x.id === id || x.id.startsWith(id + "."))
        .reduce((sum, x) => sum + (own[x.id]?.documents || 0), 0);
      const covered = (id: string) => selected.some(s => id === s || id.startsWith(s + "."));
      const toggle = (id: string, on: boolean) => update(dim,
        on ? [...selected.filter(s => !s.startsWith(id + ".")), id] : selected.filter(s => s !== id), none);
      const node = (item: { id: string; name: string }) => {
        const children = items.filter(x => x.id.startsWith(item.id + ".") && x.id.split(".").length === item.id.split(".").length + 1);
        const inherited = covered(item.id) && !selected.includes(item.id);
        const box = <label className="check"><input type="checkbox" checked={covered(item.id)} disabled={inherited}
          onChange={e => toggle(item.id, e.target.checked)} />{item.name}<small>{count(total(item.id))} 篇</small></label>;
        return <li key={item.id}>{children.length ? <details><summary>{box}</summary><ul>{children.map(node)}</ul></details> : box}</li>;
      };
      const names = selected.map(id => items.find(x => x.id === id)?.name || id).concat(none ? ["未标注"] : []);
      return <details key={dim} className="tag-dimension">
        <summary>{label}{names.length ? `：${names.join("、")}` : ""}</summary>
        <label className="check"><input type="checkbox" checked={none} onChange={e => update(dim, selected, e.target.checked)} />
          未标注<small>{count(own[""]?.documents)} 篇</small></label>
        <ul>{items.filter(x => !x.id.includes(".")).map(node)}</ul>
      </details>;
    })}
  </div>;
}
