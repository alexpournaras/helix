import { Unlink } from "lucide-react";
import { Card, Empty } from "../components/ui.jsx";

export function NotFound({ link }) {
  return (
    <Card>
      <Empty icon={Unlink} title="There’s no page at this address" action={<a className="btn btn-primary" {...link("/")}>Go to dashboard</a>}>
        The link may be mistyped, or the page may have moved.
      </Empty>
    </Card>
  );
}
