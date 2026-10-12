import { useEffect, useRef, useState } from 'react'
import { message } from 'antd'
import { InboxOutlined, LoadingOutlined } from '@ant-design/icons'
import { uploadSkillPackage, pollUploadJob, UPLOAD_STAGE_LABELS } from '../../api/skill'
import './index.css'

export default function SkillPackageUpload({ onDone }: { onDone: () => void }) {
  const lock = useRef(false)
  const mounted = useRef(true)
  const refreshTimer = useRef<number>()
  const onDoneRef = useRef(onDone)
  onDoneRef.current = onDone
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; window.clearTimeout(refreshTimer.current) } }, [])
  const [dragActive, setDragActive] = useState(false)
  const [uploading, setUploading] = useState(false)
  // 异步上传作业的当前阶段文案（受理 / 解压落盘 / 同步注册）
  const [uploadStage, setUploadStage] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)

  // 递归读取拖入文件夹的全部文件（保留包内相对路径）
  const readEntry = (entry: any, prefix: string, out: { file: File; path: string }[]): Promise<void> =>
    new Promise((resolve, reject) => {
      if (entry.isFile) {
        entry.file(
          (f: File) => { out.push({ file: f, path: prefix + f.name }); resolve() },
          reject
        )
      } else if (entry.isDirectory) {
        const reader = entry.createReader()
        const readBatch = () => {
          reader.readEntries(async (batch: any[]) => {
            try {
              if (batch.length === 0) { resolve(); return }
              for (const e of batch) await readEntry(e, prefix + entry.name + '/', out)
              readBatch() // readEntries 每次最多返回100条，读到空为止
            } catch (err) { reject(err) }
          }, reject)
        }
        readBatch()
      } else {
        resolve()
      }
    })

  // 上传是异步作业：后端受理即返回 job_id，这里轮询阶段直到终态。
  // 中文展示信息与包摘要由后台富化线程继续生成，作业完成后再补刷一次列表即可看到。
  const runUpload = async (submit: () => Promise<{ job_id: number }>) => {
    lock.current = true
    setUploading(true)
    setUploadStage(UPLOAD_STAGE_LABELS.pending)
    try {
      const accepted = await submit()
      const job = await pollUploadJob(accepted.job_id, {
        onProgress: (j) => mounted.current && setUploadStage(`${UPLOAD_STAGE_LABELS[j.status]}：${j.package_name}`),
      })
      if (job.status === 'done') {
        message.success(job.message || '上传成功')
        if (mounted.current) onDoneRef.current()
        window.clearTimeout(refreshTimer.current)
        refreshTimer.current = window.setTimeout(() => { if (mounted.current) onDoneRef.current() }, 20000)
      } else {
        message.error(job.message || job.error || '上传失败')
      }
    } catch (err: any) {
      message.error(err?.response?.data?.detail || err?.message || '上传失败')
    } finally {
      lock.current = false
      if (mounted.current) setUploading(false)
      if (mounted.current) setUploadStage('')
    }
  }

  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault()
    setDragActive(false)
    if (lock.current) return
    const zipFile = Array.from(e.dataTransfer.files).find((f) => f.name.toLowerCase().endsWith('.zip'))
    const entry = (e.dataTransfer.items?.[0] as any)?.webkitGetAsEntry?.()
    let submit: (() => Promise<{ job_id: number }>) | null = null
    lock.current = true
    setUploading(true)
    try {
      if (entry?.isDirectory) {
        const collected: { file: File; path: string }[] = []
        await readEntry(entry, '', collected)
        if (collected.length === 0) { message.error('文件夹为空'); return }
        if (!collected.some((c) => c.path.split('/').pop()?.toUpperCase() === 'SKILL.MD')) {
          message.error('技能包必须包含 SKILL.md 文件')
          return
        }
        const files = collected.map((c) => c.file)
        const paths = collected.map((c) => c.path)
        submit = () => uploadSkillPackage(files, paths)
      } else if (zipFile) {
        submit = () => uploadSkillPackage([zipFile], [])
      } else {
        message.error('请拖入技能文件夹或 .zip 包')
        return
      }
    } catch (err: any) {
      message.error(err?.response?.data?.detail || err?.message || '读取上传内容失败')
    } finally {
      // 前置校验没过（提前 return）时在这里复位；进入作业流程后交给 runUpload 接管
      if (!submit) { lock.current = false; if (mounted.current) setUploading(false) }
    }
    if (submit) await runUpload(submit)
  }

  const handleZipSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    if (lock.current) { message.warning('已有上传正在进行，请稍候'); return }
    await runUpload(() => uploadSkillPackage([file], []))
  }


  return <button type="button" className={`admin-skill-upload${dragActive ? ' active' : ''}`} aria-label="点击或拖拽上传系统技能包" aria-disabled={uploading}
    onClick={() => { if (!lock.current) fileInputRef.current?.click() }}
    onDragOver={e => { e.preventDefault(); if (!lock.current) setDragActive(true) }}
    onDragLeave={e => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragActive(false) }}
    onDrop={e => void handleDrop(e)}>
    {uploading ? <LoadingOutlined /> : <InboxOutlined />}
    <strong>{uploading ? '技能包上传处理中' : '拖拽技能包到此处（文件夹或 .zip）'}</strong>
    <span aria-live="polite">{uploading ? uploadStage : '点击选择 .zip 文件，或将完整文件夹拖入上传'}</span>
    <input ref={fileInputRef} type="file" accept=".zip" hidden aria-label="选择系统技能包ZIP" onClick={e => e.stopPropagation()} onChange={e => void handleZipSelect(e)} />
  </button>
}
