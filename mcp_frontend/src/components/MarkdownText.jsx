import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export default function MarkdownText({ text }) {
  return (
    <div className="prose-chat text-sm leading-relaxed break-words">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}
