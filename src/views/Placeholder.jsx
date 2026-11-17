import { Card, Empty } from "../components/ui.jsx";

export function Placeholder({ route, link }) {
  return (
    <Card>
      <Empty
        icon={route.icon}
        title={`${route.label} isn’t built yet`}
        action={<a className="btn" {...link("/integrations")}>View integrations</a>}
      >
        {route.blurb}
      </Empty>
    </Card>
  );
}
