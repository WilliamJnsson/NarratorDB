"use client";

import { useEffect, useState } from "react";
import type { SectionLink } from "./components";

export function MotionController() {
  useEffect(() => {
    const nodes = Array.from(document.querySelectorAll<HTMLElement>("[data-reveal]"));
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      nodes.forEach((node) => node.classList.add("is-visible"));
      return;
    }
    nodes.forEach((node) => node.classList.add("reveal-ready"));
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -10%", threshold: 0.08 });
    nodes.forEach((node) => observer.observe(node));
    return () => observer.disconnect();
  }, []);
  return null;
}

export function SectionNav({ sections }: { sections: SectionLink[] }) {
  const [active, setActive] = useState(sections[0]?.id ?? "");
  useEffect(() => {
    const nodes = sections.map(({ id }) => document.getElementById(id)).filter(Boolean) as HTMLElement[];
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (visible) setActive(visible.target.id);
    }, { rootMargin: "-22% 0px -64%", threshold: [0, .1, .3] });
    nodes.forEach((node) => observer.observe(node));
    return () => observer.disconnect();
  }, [sections]);
  return <nav className="section-nav" aria-label="Page sections"><div className="shell section-nav-inner">{sections.map((section) => <a href={`#${section.id}`} aria-current={active === section.id ? "location" : undefined} key={section.id}><span>{section.number}</span>{section.label}</a>)}</div></nav>;
}
