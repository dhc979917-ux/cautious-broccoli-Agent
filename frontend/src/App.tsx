import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { ConfigProvider, theme, Spin } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { useEffect, lazy, Suspense } from 'react'
import { PRIMARY_COLOR } from './constants/theme'

// 路由级懒加载：每个页面独立 chunk，登录页只加载 react-vendor + antd-vendor，
// 不再下载 echarts/vis-network/d3-force 等仅特定页面使用的重型库
const Login = lazy(() => import('./pages/Login'))
const ForcePasswordChange = lazy(() => import('./pages/ForcePasswordChange'))
const Home = lazy(() => import('./pages/Home'))
const Chat = lazy(() => import('./pages/Chat'))
const SkillCommunityAdmin = lazy(() => import('./pages/SkillCommunityAdmin'))
const SkillSquare = lazy(() => import('./pages/SkillSquare'))
const Files = lazy(() => import('./pages/Files'))
const Admin = lazy(() => import('./pages/Admin'))
const AdminHome = lazy(() => import('./pages/AdminHome'))
const Knowledge = lazy(() => import('./pages/Knowledge'))
const McpTools = lazy(() => import('./pages/McpTools'))
const McpSourcePage = lazy(() => import('./pages/McpTools/Source'))
const Monitoring = lazy(() => import('./pages/Monitoring'))
const ScheduledTasks = lazy(() => import('./pages/ScheduledTasks'))
const Profile = lazy(() => import('./pages/Profile'))
const Users = lazy(() => import('./pages/Users'))
const PeopleAdmin = lazy(() => import('./pages/PeopleAdmin'))
const ModelAdmin = lazy(() => import('./pages/ModelAdmin'))
const AgentConfig = lazy(() => import('./pages/AgentConfig'))
const SkillVersions = lazy(() => import('./pages/SkillVersions'))
const MemoryAdmin = lazy(() => import('./pages/MemoryAdmin'))
const Operations = lazy(() => import('./pages/Operations'))
const GovernanceLogs = lazy(() => import('./pages/GovernanceLogs'))
const ChatPreview = lazy(() => import('./pages/ChatPreview'))
import Layout from './components/Layout'
import ErrorBoundary from './components/ErrorBoundary'
import { useAuthStore } from './store/authStore'

function RequireAuth({ children }: { children: React.ReactNode }) {
  const token = useAuthStore((state) => state.token)
  const mustChangePassword = useAuthStore((state) => Boolean(state.user?.must_change_password))
  if (!token) return <Navigate to="/login" replace />
  if (mustChangePassword) return <Navigate to="/force-password-change" replace />
  return <>{children}</>
}

function RequirePasswordChange() {
  const token = useAuthStore((state) => state.token)
  const mustChangePassword = useAuthStore((state) => Boolean(state.user?.must_change_password))
  if (!token) return <Navigate to="/login" replace />
  if (!mustChangePassword) return <Navigate to="/" replace />
  return <ForcePasswordChange />
}

export default function App() {
  const themeMode = useAuthStore((state) => state.theme) || 'light'

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', themeMode)
  }, [themeMode])

  return (
    <ErrorBoundary>
      <ConfigProvider
        locale={zhCN}
      theme={{
        algorithm: themeMode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: {
          colorPrimary: PRIMARY_COLOR,
          borderRadius: 8,
          // antd 弹层（Modal/Select 下拉/Tooltip 等）基准层级需高于侧边栏 --z-sidebar:1040，
          // 否则弹窗和下拉菜单会被压在侧边栏之下，出现下拉框点不开的问题
          zIndexPopupBase: 1100,
        },
      }}
    >
      <BrowserRouter>
        <Suspense fallback={<div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh' }}><Spin size="large" /></div>}>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/force-password-change" element={<RequirePasswordChange />} />

          <Route
            path="/"
            element={
              <RequireAuth>
                <Layout>
                  <Home />
                </Layout>
              </RequireAuth>
            }
          />
          <Route
            path="/chat"
            element={
              <RequireAuth>
                <Layout>
                  <Chat />
                </Layout>
              </RequireAuth>
            }
          />
          <Route
            path="/chat/:sessionId"
            element={
              <RequireAuth>
                <Layout>
                  <Chat />
                </Layout>
              </RequireAuth>
            }
          />
          <Route
            path="/skills"
            element={
              <RequireAuth>
                <Layout>
                  <SkillSquare />
                </Layout>
              </RequireAuth>
            }
          />
          <Route
            path="/files"
            element={
              <RequireAuth>
                <Layout>
                  <Files />
                </Layout>
              </RequireAuth>
            }
          />
          <Route
            path="/admin"
            element={
              <RequireAuth>
                <Layout>
                  <AdminHome />
                </Layout>
              </RequireAuth>
            }
          />
          <Route
            path="/admin/agents"
            element={<RequireAuth><Layout><Admin view="agents" /></Layout></RequireAuth>}
          />
          <Route
            path="/admin/agents/:agentId"
            element={<RequireAuth><Layout><AgentConfig /></Layout></RequireAuth>}
          />
          <Route path="/admin/skill-community" element={<RequireAuth><Layout><SkillCommunityAdmin /></Layout></RequireAuth>} />
          <Route
            path="/admin/skills"
            element={<RequireAuth><Layout><Admin view="skills" /></Layout></RequireAuth>}
          />
          <Route
            path="/admin/skills/:skillId"
            element={<RequireAuth><Layout><SkillVersions /></Layout></RequireAuth>}
          />
          <Route path="/admin/skill-packages/:packageId" element={<RequireAuth><Layout><SkillVersions /></Layout></RequireAuth>} />
          <Route
            path="/admin/models/permissions"
            element={<RequireAuth><Navigate to="/admin/models" replace /></RequireAuth>}
          />
          <Route
            path="/admin/mcp"
            element={<RequireAuth><Layout><McpTools management /></Layout></RequireAuth>}
          />
          <Route path="/admin/mcp/:serverId/source/:toolName" element={<RequireAuth><Layout><McpSourcePage /></Layout></RequireAuth>} />
          <Route path="/admin/memories" element={<RequireAuth><Layout><MemoryAdmin /></Layout></RequireAuth>} />
          <Route path="/admin/operations" element={<RequireAuth><Layout><Operations /></Layout></RequireAuth>} />
          <Route path="/admin/audit" element={<RequireAuth><Layout><GovernanceLogs kind="audit" /></Layout></RequireAuth>} />
          <Route path="/admin/traces" element={<RequireAuth><Layout><GovernanceLogs kind="trace" /></Layout></RequireAuth>} />
          <Route
            path="/admin/users"
            element={
              <RequireAuth>
                <Layout>
                  <Navigate to="/admin/people" replace />
                </Layout>
              </RequireAuth>
            }
          />
          <Route
            path="/admin/people"
            element={
              <RequireAuth>
                <Layout>
                  <PeopleAdmin />
                </Layout>
              </RequireAuth>
            }
          />
          <Route
            path="/admin/models"
            element={
              <RequireAuth>
                <Layout>
                  <ModelAdmin />
                </Layout>
              </RequireAuth>
            }
          />

          <Route
            path="/knowledge"
            element={
              <RequireAuth>
                <Layout>
                  <Knowledge />
                </Layout>
              </RequireAuth>
            }
          />
          <Route
            path="/mcp"
            element={
              <RequireAuth>
                <Layout>
                  <McpTools />
                </Layout>
              </RequireAuth>
            }
          />
          <Route
            path="/monitoring"
            element={
              <RequireAuth>
                <Layout>
                  <Monitoring />
                </Layout>
              </RequireAuth>
            }
          />
          <Route
            path="/monitoring/user/:userId"
            element={
              <RequireAuth>
            <Layout>
              <Monitoring />
            </Layout>
          </RequireAuth>
        }
      />
          <Route
            path="/monitoring/user/:userId/chat"
            element={
              <RequireAuth>
                <Layout>
                  <ChatPreview />
                </Layout>
              </RequireAuth>
            }
          />
      <Route
        path="/scheduled-tasks"
            element={
              <RequireAuth>
                <Layout>
                  <ScheduledTasks />
                </Layout>
              </RequireAuth>
            }
          />
          <Route
            path="/profile"
            element={
              <RequireAuth>
                <Layout>
                  <Profile />
                </Layout>
              </RequireAuth>
            }
          />
          <Route
            path="/users"
            element={
              <RequireAuth>
                <Layout>
                  <Users />
                </Layout>
              </RequireAuth>
            }
          />

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </Suspense>
      </BrowserRouter>
    </ConfigProvider>
    </ErrorBoundary>
  )
}
