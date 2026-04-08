import React, { useState, useRef, useEffect, useCallback } from 'react';
import {
  Page,
  PageSection,
  Title,
  Split,
  SplitItem,
  Card,
  CardTitle,
  CardBody,
  TextInput,
  Button,
  FormGroup,
  DropEvent,
  MultipleFileUpload,
  MultipleFileUploadMain,
  MultipleFileUploadStatus,
  MultipleFileUploadStatusItem,
  Progress,
  ProgressVariant,
  Label,
  Select,
  SelectOption,
  MenuToggle,
  MenuToggleElement,
  TextArea,
  Flex,
  FlexItem,
  Divider,
  EmptyState,
  EmptyStateBody,
  Spinner,
} from '@patternfly/react-core';
import {
  PaperPlaneIcon,
  TrashIcon,
  PlusCircleIcon,
  BookOpenIcon,
} from '@patternfly/react-icons';

const API_BASE = window.location.hostname === 'localhost' ? '' : '/api';

const MODELS = [
  { value: 'qwen3-4b-instruct', label: 'Qwen3 4B Instruct' },
  { value: 'llama-3-1-8b-instruct-fp8', label: 'Llama 3.1 8B FP8' },
  { value: 'mistral-small-24b-fp8', label: 'Mistral Small 24B FP8' },
  { value: 'phi-4-instruct-w8a8', label: 'Phi-4 Instruct W8A8' },
];

interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  sources?: string[];
}

interface IngestJob {
  status: string;
  progress?: number;
  filename?: string;
  total_chunks?: number;
  chunks_stored?: number;
  error?: string;
}

interface DocEntry {
  doc_id: string;
  filename: string;
  ingest_status: string;
}

export const App: React.FC = () => {
  // ── Notebook state ──
  const [notebookName, setNotebookName] = useState('');
  const [notebookId, setNotebookId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  // ── Documents state ──
  const [documents, setDocuments] = useState<DocEntry[]>([]);
  const [uploading, setUploading] = useState(false);
  const [ingestJobs, setIngestJobs] = useState<Record<string, IngestJob>>({});
  const ingestPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Chat state ──
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [query, setQuery] = useState('');
  const [selectedModel, setSelectedModel] = useState(MODELS[0].value);
  const [modelSelectOpen, setModelSelectOpen] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const chatEndRef = useRef<HTMLDivElement>(null);

  // Auto-scroll chat
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // ── Ingest polling ──
  const startIngestPoll = useCallback((nbId: string) => {
    if (ingestPollRef.current) clearInterval(ingestPollRef.current);
    ingestPollRef.current = setInterval(async () => {
      try {
        const resp = await fetch(`${API_BASE}/notebooks/${nbId}/ingest-status`);
        if (!resp.ok) return;
        const data = await resp.json();
        setIngestJobs(data.jobs || {});

        const allDone = Object.values(data.jobs as Record<string, IngestJob>).every(
          (j) => j.status === 'completed' || j.status === 'failed',
        );
        if (allDone && Object.keys(data.jobs).length > 0) {
          if (ingestPollRef.current) clearInterval(ingestPollRef.current);
          ingestPollRef.current = null;
        }
      } catch {
        /* ignore transient errors */
      }
    }, 3000);
  }, []);

  useEffect(() => {
    return () => {
      if (ingestPollRef.current) clearInterval(ingestPollRef.current);
    };
  }, []);

  // ── Notebook CRUD ──
  const createNotebook = async () => {
    if (!notebookName.trim()) return;
    setCreating(true);
    try {
      const resp = await fetch(`${API_BASE}/notebooks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: notebookName.trim() }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setNotebookId(data.notebook_id);
    } finally {
      setCreating(false);
    }
  };

  const refreshDocuments = async (nbId: string) => {
    const resp = await fetch(`${API_BASE}/notebooks/${nbId}/documents`);
    if (resp.ok) {
      const data = await resp.json();
      setDocuments(data.documents || []);
    }
  };

  // ── File upload ──
  const handleFileDrop = async (_event: DropEvent, files: File[]) => {
    if (!notebookId || files.length === 0) return;
    setUploading(true);
    try {
      for (const file of files) {
        const formData = new FormData();
        formData.append('file', file);
        await fetch(`${API_BASE}/notebooks/${notebookId}/documents`, {
          method: 'POST',
          body: formData,
        });
      }
      await refreshDocuments(notebookId);
      startIngestPoll(notebookId);
    } finally {
      setUploading(false);
    }
  };

  // ── SSE chat via fetch + ReadableStream ──
  const sendMessage = async () => {
    if (!query.trim() || !notebookId || streaming) return;
    const userQuery = query.trim();
    setQuery('');
    setStreaming(true);

    let assistantIdx = 0;
    setMessages((prev) => {
      assistantIdx = prev.length + 1;
      return [...prev, { role: 'user', content: userQuery },
                       { role: 'assistant', content: '', sources: [] }];
    });

    try {
      const response = await fetch(`${API_BASE}/notebooks/${notebookId}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify({ query: userQuery, model: selectedModel }),
      });

      if (!response.ok || !response.body) {
        setMessages((prev) => {
          const updated = [...prev];
          updated[assistantIdx] = {
            role: 'assistant',
            content: `Error: HTTP ${response.status}`,
          };
          return updated;
        });
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      const collectedSources: string[] = [];

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const payload = line.slice(6).trim();
          if (payload === '[DONE]') continue;

          try {
            const parsed = JSON.parse(payload);

            // Extract token text — handle multiple payload shapes
            let token = '';
            if (parsed?.event?.delta?.text) {
              token = parsed.event.delta.text;
            } else if (parsed?.delta?.content) {
              token = parsed.delta.content;
            } else if (parsed?.event?.payload?.text) {
              token = parsed.event.payload.text;
            } else if (typeof parsed?.text === 'string') {
              token = parsed.text;
            }

            if (token) {
              setMessages((prev) => {
                const updated = [...prev];
                const msg = updated[assistantIdx];
                updated[assistantIdx] = { ...msg, content: msg.content + token };
                return updated;
              });
            }

            // Extract source citations from retrieved_context
            if (
              parsed?.event?.payload?.type === 'inference_result' &&
              parsed?.event?.payload?.retrieved_context
            ) {
              for (const ctx of parsed.event.payload.retrieved_context) {
                if (ctx.source_uri && !collectedSources.includes(ctx.source_uri)) {
                  collectedSources.push(ctx.source_uri);
                }
                if (ctx.source && !collectedSources.includes(ctx.source)) {
                  collectedSources.push(ctx.source);
                }
              }
            }
          } catch {
            /* skip malformed JSON lines */
          }
        }
      }

      // Attach sources once streaming is complete
      if (collectedSources.length > 0) {
        setMessages((prev) => {
          const updated = [...prev];
          updated[assistantIdx] = { ...updated[assistantIdx], sources: collectedSources };
          return updated;
        });
      }
    } finally {
      setStreaming(false);
    }
  };

  const handleChatKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const clearChat = () => {
    setMessages([]);
  };

  // ── Helpers ──
  const ingestVariant = (status: string): ProgressVariant | undefined => {
    if (status === 'completed') return ProgressVariant.success;
    if (status === 'failed') return ProgressVariant.danger;
    return undefined;
  };

  // ── Render ──
  return (
    <Page>
      <PageSection variant="light">
        <Title headingLevel="h1" size="2xl">
          <BookOpenIcon /> NotebookLM
        </Title>
      </PageSection>

      <PageSection>
        <Split hasGutter>
          {/* ═══════════ Panel 1: Notebook Setup ═══════════ */}
          <SplitItem style={{ flex: 1, minWidth: 360 }}>
            <Card>
              <CardTitle>Notebook Setup</CardTitle>
              <CardBody>
                {!notebookId ? (
                  <FormGroup label="Notebook name" fieldId="nb-name">
                    <Flex>
                      <FlexItem grow={{ default: 'grow' }}>
                        <TextInput
                          id="nb-name"
                          value={notebookName}
                          onChange={(_e, val) => setNotebookName(val)}
                          placeholder="My research notebook"
                          onKeyDown={(e) => e.key === 'Enter' && createNotebook()}
                        />
                      </FlexItem>
                      <FlexItem>
                        <Button
                          icon={<PlusCircleIcon />}
                          onClick={createNotebook}
                          isLoading={creating}
                          isDisabled={!notebookName.trim() || creating}
                        >
                          Create notebook
                        </Button>
                      </FlexItem>
                    </Flex>
                  </FormGroup>
                ) : (
                  <>
                    <FormGroup label="Active notebook" fieldId="nb-active">
                      <Label color="blue">{notebookId}</Label>
                    </FormGroup>

                    <Divider style={{ margin: '16px 0' }} />

                    <FormGroup label="Upload documents" fieldId="nb-upload">
                      <MultipleFileUpload
                        onFileDrop={handleFileDrop}
                        dropzoneProps={{
                          accept: {
                            'application/pdf': ['.pdf'],
                            'text/plain': ['.txt'],
                            'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
                              ['.docx'],
                          },
                        }}
                      >
                        <MultipleFileUploadMain
                          titleIcon={uploading ? <Spinner size="md" /> : undefined}
                          titleText={uploading ? 'Uploading...' : 'Drag and drop files here'}
                          infoText="Accepted: PDF, TXT, DOCX"
                        />
                        {documents.length > 0 && (
                          <MultipleFileUploadStatus
                            statusToggleText={`${documents.length} file(s) uploaded`}
                          >
                            {documents.map((doc) => (
                              <MultipleFileUploadStatusItem
                                key={doc.doc_id}
                                fileName={doc.filename}
                                fileSize={0}
                              />
                            ))}
                          </MultipleFileUploadStatus>
                        )}
                      </MultipleFileUpload>
                    </FormGroup>

                    {/* Ingest status */}
                    {Object.keys(ingestJobs).length > 0 && (
                      <>
                        <Divider style={{ margin: '16px 0' }} />
                        <FormGroup label="Ingest status" fieldId="nb-ingest">
                          {Object.entries(ingestJobs).map(([docId, job]) => (
                            <div key={docId} style={{ marginBottom: 12 }}>
                              <div style={{ marginBottom: 4, fontSize: 13 }}>
                                {job.filename || docId} &mdash;{' '}
                                <Label
                                  color={
                                    job.status === 'completed'
                                      ? 'green'
                                      : job.status === 'failed'
                                        ? 'red'
                                        : 'blue'
                                  }
                                >
                                  {job.status}
                                </Label>
                              </div>
                              <Progress
                                value={job.progress || 0}
                                title=""
                                size="sm"
                                variant={ingestVariant(job.status)}
                              />
                              {job.error && (
                                <div style={{ color: '#c9190b', fontSize: 12, marginTop: 4 }}>
                                  {job.error}
                                </div>
                              )}
                              {job.chunks_stored && (
                                <div style={{ fontSize: 12, marginTop: 4, color: '#6a6e73' }}>
                                  {job.chunks_stored} chunks indexed
                                </div>
                              )}
                            </div>
                          ))}
                        </FormGroup>
                      </>
                    )}
                  </>
                )}
              </CardBody>
            </Card>
          </SplitItem>

          {/* ═══════════ Panel 2: Chat Interface ═══════════ */}
          <SplitItem style={{ flex: 2, minWidth: 480 }}>
            <Card style={{ display: 'flex', flexDirection: 'column', height: '80vh' }}>
              <CardTitle>
                <Flex justifyContent={{ default: 'justifyContentSpaceBetween' }}>
                  <FlexItem>Chat</FlexItem>
                  <FlexItem>
                    <Flex spaceItems={{ default: 'spaceItemsSm' }}>
                      <FlexItem>
                        <Select
                          isOpen={modelSelectOpen}
                          selected={selectedModel}
                          onSelect={(_e, val) => {
                            setSelectedModel(val as string);
                            setModelSelectOpen(false);
                          }}
                          onOpenChange={setModelSelectOpen}
                          toggle={(toggleRef: React.Ref<MenuToggleElement>) => (
                            <MenuToggle
                              ref={toggleRef}
                              onClick={() => setModelSelectOpen(!modelSelectOpen)}
                              isExpanded={modelSelectOpen}
                              style={{ minWidth: 200 }}
                            >
                              {MODELS.find((m) => m.value === selectedModel)?.label}
                            </MenuToggle>
                          )}
                        >
                          {MODELS.map((m) => (
                            <SelectOption key={m.value} value={m.value}>
                              {m.label}
                            </SelectOption>
                          ))}
                        </Select>
                      </FlexItem>
                      <FlexItem>
                        <Button
                          variant="plain"
                          icon={<TrashIcon />}
                          onClick={clearChat}
                          aria-label="Clear conversation"
                          isDisabled={messages.length === 0}
                        />
                      </FlexItem>
                    </Flex>
                  </FlexItem>
                </Flex>
              </CardTitle>

              <CardBody style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
                {messages.length === 0 && !notebookId ? (
                  <EmptyState>
                    <EmptyStateBody>
                      Create a notebook and upload documents to start chatting.
                    </EmptyStateBody>
                  </EmptyState>
                ) : messages.length === 0 ? (
                  <EmptyState>
                    <EmptyStateBody>
                      Upload a document, wait for ingest, then ask a question.
                    </EmptyStateBody>
                  </EmptyState>
                ) : (
                  messages.map((msg, i) => (
                    <div
                      key={i}
                      style={{
                        display: 'flex',
                        justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start',
                        marginBottom: 12,
                      }}
                    >
                      <div
                        style={{
                          maxWidth: '75%',
                          padding: '10px 14px',
                          borderRadius: 8,
                          background: msg.role === 'user' ? '#0066cc' : '#f0f0f0',
                          color: msg.role === 'user' ? '#fff' : '#151515',
                          whiteSpace: 'pre-wrap',
                          wordBreak: 'break-word',
                          fontSize: 14,
                          lineHeight: 1.5,
                        }}
                      >
                        {msg.content || (streaming && i === messages.length - 1 ? (
                          <Spinner size="sm" />
                        ) : null)}
                        {msg.sources && msg.sources.length > 0 && (
                          <div
                            style={{
                              marginTop: 8,
                              paddingTop: 8,
                              borderTop: '1px solid #d2d2d2',
                              fontSize: 12,
                            }}
                          >
                            <strong>Sources:</strong>
                            <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
                              {msg.sources.map((src, si) => (
                                <li key={si}>
                                  <a
                                    href={src}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    style={{ color: '#06c' }}
                                  >
                                    {src}
                                  </a>
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}
                      </div>
                    </div>
                  ))
                )}
                <div ref={chatEndRef} />
              </CardBody>

              <div style={{ padding: '12px 16px', borderTop: '1px solid #d2d2d2' }}>
                <Flex>
                  <FlexItem grow={{ default: 'grow' }}>
                    <TextArea
                      value={query}
                      onChange={(_e, val) => setQuery(val)}
                      onKeyDown={handleChatKeyDown}
                      placeholder={
                        notebookId
                          ? 'Ask a question about your documents...'
                          : 'Create a notebook first'
                      }
                      isDisabled={!notebookId || streaming}
                      rows={1}
                      autoResize
                      aria-label="Chat input"
                    />
                  </FlexItem>
                  <FlexItem alignSelf={{ default: 'alignSelfFlexEnd' }}>
                    <Button
                      icon={<PaperPlaneIcon />}
                      onClick={sendMessage}
                      isDisabled={!notebookId || !query.trim() || streaming}
                      isLoading={streaming}
                    >
                      Send
                    </Button>
                  </FlexItem>
                </Flex>
              </div>
            </Card>
          </SplitItem>
        </Split>
      </PageSection>
    </Page>
  );
};
