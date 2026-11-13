export interface paths {
    "/api/v1/artifacts/{artifact_id}/content": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Artifact Content
         * @description 按登记形态读产物（spec §10、#222 §7）。
         *
         *     文本产物回纯文本（1 MiB 上限），媒体产物回字节并支持单段 Range——形态来自
         *     ``workbench_store`` 的登记表，这里不认文件名。``artifact_id`` 是
         *     ``<session_id>/<产物名>``：两边都可能含斜杠（``<主题>/GEN00N`` +
         *     ``segments/shot-01.md``），切分按登记表的最长合法后缀走，``:path`` 沿用会话
         *     路由的口径。越界与不存在一律 404 ``artifact_not_found``。
         */
        get: operations["get_artifact_content_api_v1_artifacts__artifact_id__content_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        /**
         * Head Artifact Content
         * @description 探测产物：与 GET 同头、无体（#222 §6.2 的 HEAD 一格同样适用于成片）。
         */
        head: operations["head_artifact_content_api_v1_artifacts__artifact_id__content_head"];
        patch?: never;
        trace?: never;
    };
    "/api/v1/auth/token": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Auth Token */
        get: operations["get_auth_token_api_v1_auth_token_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/diagnostics/bundle": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Diagnostics Bundle
         * @description 导出诊断包（``application/zip`` + ``attachment``，spec §8.2）。
         */
        get: operations["get_diagnostics_bundle_api_v1_diagnostics_bundle_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Health */
        get: operations["get_health_api_v1_health_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/media/{asset_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Media
         * @description 读素材字节；``Range`` 只支持单段（#222 §6.2）。
         */
        get: operations["get_media_api_v1_media__asset_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        /**
         * Head Media
         * @description 探测素材：头与 GET 一致、无体；供播放器决定要不要发 Range。
         */
        head: operations["head_media_api_v1_media__asset_id__head"];
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Sessions */
        get: operations["list_sessions_api_v1_sessions_get"];
        put?: never;
        /** Create Session */
        post: operations["create_session_api_v1_sessions_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Session */
        get: operations["get_session_api_v1_sessions__session_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/action-items": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Action Items
         * @description 会话的全部待办（含已决议的）：创建与决议成对可查，前端不必自己攒。
         */
        get: operations["list_action_items_api_v1_sessions__session_id__action_items_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/action-items/{item_id}/resolve": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Resolve Action Item
         * @description 提交人工决定：``approve`` 走后续步骤（base 进阶段 2，ref 先进素材循环），
         *     ``revise`` 从声明层次重跑阶段 1。
         *
         *     其余人工待办复用同一个端点、靠 body 分型（D4 §9）：带 ``frames`` 即帧提交；段视频
         *     （``video_asset_id``，D6 §8）、素材图（``asset_id``，D7 §6.3）与导演台开场首帧
         *     （``decision=submit_start_image`` + ``start_asset_id``，D8 §7.1）同理。kind 与载荷
         *     错配一律 422（``frame_invalid`` / ``segment_video_invalid`` / ``asset_image_invalid``）。
         *
         *     状态判定与「决议 + 起任务」的原子性都在 ``JobRunner.resolve`` 的会话临界区里，
         *     这里只把请求体归一成内部词汇，不先查一遍状态（两处判据会在并发下各说各话）。
         */
        post: operations["resolve_action_item_api_v1_sessions__session_id__action_items__item_id__resolve_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/artifacts": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Artifacts
         * @description 会话的产物清单：与详情里的 ``artifacts`` 同一份来源（目录现扫）。
         */
        get: operations["list_artifacts_api_v1_sessions__session_id__artifacts_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/assets": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Assets
         * @description 会话的素材清单：与详情里的 ``assets`` 同一份来源（目录现扫，无登记表）。
         */
        get: operations["list_assets_api_v1_sessions__session_id__assets_get"];
        put?: never;
        /**
         * Upload Asset
         * @description 收一份素材：边写边判超限、按字节定类型、按内容哈希去重（#222 §5）。
         *
         *     上传**不改会话状态、不产生事件**（D3-AC13）：素材是不可变的旁挂资源，与运行中的
         *     任务并行安全。
         */
        post: operations["upload_asset_api_v1_sessions__session_id__assets_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/assets/{asset_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /**
         * Delete Asset
         * @description 删掉一条参考素材登记与它的正本（D7 spec §4.2）。
         *
         *     spec §4.2 要求「循环开始前可删素材」，§12 又写「不新增端点」——两句话冲突，
         *     用户 2026-11-13 裁定**新增删除端点**。``asset_id`` 与 ``GET /api/v1/media/…``
         *     同形（``<会话标识>/<16 位内容哈希>``），归属校验在存储层：不属于本会话与不存在
         *     同码 404，不给出探测他会话素材的 oracle。
         *
         *     成功回 204 空体；**不发事件**（素材增删仍不是事件，D3-AC13）。
         */
        delete: operations["delete_asset_api_v1_sessions__session_id__assets__asset_id__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Stream Events
         * @description 订阅会话事件流。
         *
         *     **快照优先**（spec §8）：先 ``GET /api/v1/sessions/{session_id}`` 拿快照，再订阅
         *     本流；补不上缺口时按响应头的 ``X-Resync-Required`` 重取快照。
         */
        get: operations["stream_events_api_v1_sessions__session_id__events_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/jobs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Start Job
         * @description 起一次 ``advance``：202 返回任务对象，执行在后台任务里。
         *
         *     闸门与单写入者判定在 ``JobRunner.advance`` 的会话临界区里完成；这里不再判一遍，
         *     否则「先查后写」的两处判据会在并发下各说各话（D2-AC3）。
         */
        post: operations["start_job_api_v1_sessions__session_id__jobs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/jobs/{job_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Job */
        get: operations["get_job_api_v1_sessions__session_id__jobs__job_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/jobs/{job_id}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Cancel Job
         * @description 请求取消：只打标记，任务在当前节点跑完后才停（**不承诺立即停止**）。
         */
        post: operations["cancel_job_api_v1_sessions__session_id__jobs__job_id__cancel_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/sessions/{session_id}/segments/{index}/reopen": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Reopen Segment
         * @description 把一条**已通过**的 ref 段打回等待交视频（D7 §5.5 / #258）。
         *
         *     spec §12 原本写「不新增端点」，用户 2026-11-13 裁定新增这条段级端点。换段只换
         *     视频：段词与段内参考素材子集不动，重交后重过双闸门。base 会话回 409
         *     ``segment_resubmit_unsupported``（中间段换掉会断掉下一段的帧锚，ADR 0023）；
         *     未通过的段回 409 ``segment_not_passed``。
         *
         *     状态判定与落盘在 ``JobRunner.reopen_segment`` 的会话临界区里完成，这里不先查
         *     一遍状态——两处判据会在并发下各说各话。
         */
        post: operations["reopen_segment_api_v1_sessions__session_id__segments__index__reopen_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /** ActionItemOut */
        ActionItemOut: {
            /** Asset Index */
            asset_index?: number | null;
            /**
             * Created At
             * @default
             */
            created_at: string;
            /** Decision */
            decision?: string | null;
            /** End Asset Id */
            end_asset_id?: string | null;
            /** Item Id */
            item_id: string;
            /** Kind */
            kind: string;
            /**
             * Reason
             * @default
             */
            reason: string;
            /**
             * Resolved At
             * @default
             */
            resolved_at: string;
            /** Revisions */
            revisions?: components["schemas"]["RevisionOut"][];
            /** Segment Index */
            segment_index?: number | null;
            /** Start Asset Id */
            start_asset_id?: string | null;
            /** Status */
            status: string;
        };
        /**
         * ActionItemResolve
         * @description 决议载荷（spec §7 / D4 §9 / D6 §8 / D7 §6.3 / D8 §7.1）。
         *
         *     五类人工待办各收一种载荷：
         *     - ``stage1_review`` 收 ``decision``（approve/revise，revise 带 ``revisions``）；
         *     - ``frame_submission`` 收 ``frames``；
         *     - ``segment_video`` 收 ``video_asset_id``（本段视频素材）；
         *     - ``asset_image`` 收 ``asset_id``（这一件资产图的生成结果，普通素材上传得到）；
         *     - ``director_start_image`` 收 ``decision="submit_start_image"`` 与
         *       ``start_asset_id``（必填）/``end_asset_id``（可选）/``confirm_aspect_mismatch``。
         *
         *     kind 与载荷的错配由服务端判成 422（``frame_invalid`` / ``segment_video_invalid`` /
         *     ``asset_image_invalid``，不是 pydantic 的通用 422）。
         */
        ActionItemResolve: {
            /** Asset Id */
            asset_id?: string | null;
            /** Confirm Aspect Mismatch */
            confirm_aspect_mismatch?: boolean | null;
            /** Decision */
            decision?: ("approve" | "revise" | "submit_start_image") | null;
            /** End Asset Id */
            end_asset_id?: string | null;
            frames?: components["schemas"]["FrameSelection"] | null;
            /** Revisions */
            revisions?: components["schemas"]["RevisionIn"][];
            /** Start Asset Id */
            start_asset_id?: string | null;
            /** Video Asset Id */
            video_asset_id?: string | null;
        };
        /** ArtifactInfo */
        ArtifactInfo: {
            /** Artifact Id */
            artifact_id: string;
            /** Bytes */
            bytes: number;
            /** Created At */
            created_at: string;
            /** Kind */
            kind: string;
            /** Name */
            name: string;
        };
        /**
         * Asset
         * @description 上传素材的对外形状（#222 §9）。``asset_id`` 与 ``artifact_id`` 同形：
         *     ``<session_id>/<sha256 前 16 位>``——路径参数按 ``{asset_id:path}`` 承接。
         *
         *     ``number`` 只有 ref 上传（台账登记）才有值（D7 §12「返回编号与类型」）；base
         *     上传与目录现扫（列表/详情）为 ``None``。``kind`` 始终是**嗅探类型**
         *     （image/video/audio），台账里的官方三型（picture/video/audio）在 ``brief.refs``。
         */
        Asset: {
            /** Asset Id */
            asset_id: string;
            /** Bytes */
            bytes: number;
            /** Created At */
            created_at: string;
            /** Kind */
            kind: string;
            /** Name */
            name: string;
            /** Number */
            number?: number | null;
        };
        /**
         * AuthTokenResponse
         * @description 启动令牌资源：字段名即契约，前端据此缓存到内存。
         */
        AuthTokenResponse: {
            /** Token */
            token: string;
        };
        /** BriefOut */
        BriefOut: {
            /** Canvases */
            canvases?: {
                [key: string]: number[];
            };
            /**
             * Clarifications
             * @default
             */
            clarifications: string;
            /**
             * Director Cache Name
             * @default
             */
            director_cache_name: string;
            /**
             * Director Continuity Mode
             * @default
             */
            director_continuity_mode: string;
            /**
             * Director Mode
             * @default
             */
            director_mode: string;
            /**
             * Director Node Id
             * @default
             */
            director_node_id: string;
            /**
             * Draft
             * @default
             */
            draft: string;
            /** Duration */
            duration: number;
            /** Language */
            language: string;
            /** Mode */
            mode: string;
            /** Plot */
            plot: string;
            /** Refs */
            refs?: components["schemas"]["RefOut"][];
            /** Style */
            style: string;
            /** Variant */
            variant: string;
            video_output?: components["schemas"]["VideoOutputOut"] | null;
        };
        /**
         * FrameSelection
         * @description 帧槽选择（D4 spec §9）：首/尾各绑本会话一个 D3 素材，都可留空。
         *
         *     两个字段都是 ``asset_id``（``<会话标识>/<16 位内容哈希>``），不是路径；服务端
         *     负责归属与存在性校验。首尾都空＝显式跳过（降级为纯文字），不是「没带载荷」。
         */
        FrameSelection: {
            /** First */
            first?: string | null;
            /** Last */
            last?: string | null;
        };
        /**
         * HealthResponse
         * @description 健康状态资源。字段约定见 D1 spec §7.1；``data_root``–``missing_keys`` 见 #238，
         *     ``base_url`` 见 #237。
         */
        HealthResponse: {
            /** Base Url */
            base_url: string;
            /** Config Path */
            config_path: string;
            /** Data Root */
            data_root: string;
            /** Env Path */
            env_path: string;
            /** Missing Keys */
            missing_keys: string[];
            /** Service */
            service: string;
            /**
             * Status
             * @constant
             */
            status: "ok";
            /** Version */
            version: string;
        };
        /** JobSummary */
        JobSummary: {
            /**
             * Cancel Requested
             * @default false
             */
            cancel_requested: boolean;
            /** Cancelled At Node */
            cancelled_at_node?: string | null;
            /**
             * Detail
             * @default
             */
            detail: string;
            /** Error Code */
            error_code?: string | null;
            /**
             * Finished At
             * @default
             */
            finished_at: string;
            /** Job Id */
            job_id: string;
            /**
             * Kind
             * @default advance
             */
            kind: string;
            /**
             * Started At
             * @default
             */
            started_at: string;
            /** Status */
            status: string;
        };
        /**
         * ProblemDetail
         * @description 统一错误资源（RFC 9457）。
         */
        ProblemDetail: {
            /** Code */
            code?: string | null;
            /** Detail */
            detail: string;
            /** Errors */
            errors?: components["schemas"]["ProblemFieldError"][] | null;
            /** Instance */
            instance: string;
            /** Status */
            status: number;
            /** Title */
            title: string;
            /**
             * Type
             * @default about:blank
             */
            type: string;
        };
        /**
         * ProblemFieldError
         * @description 422 校验错误元素：``{loc, msg, type}``。
         */
        ProblemFieldError: {
            /** Loc */
            loc: (string | number)[];
            /** Msg */
            msg: string;
            /** Type */
            type: string;
        };
        /**
         * RefOut
         * @description 素材台账摘要（D7 spec §12）：语义名 / 官方三型 / 同类编号 / 素材标识。
         *
         *     这里回显的是**台账**（会话状态），不是上传目录的现扫结果；两者语义不同
         *     （``SessionDetail.assets`` 仍是目录现扫）。``asset_id`` 用来把摘要接到缩略图与
         *     媒体读取上。**没有本地路径字段**：工作台的素材只能走上传接口（spec §4.1）。
         */
        RefOut: {
            /** Asset Id */
            asset_id: string;
            /** Kind */
            kind: string;
            /** Name */
            name: string;
            /** Number */
            number: number;
        };
        /** RevisionIn */
        RevisionIn: {
            /** Layer */
            layer: string;
            /** Text */
            text: string;
        };
        /**
         * RevisionOut
         * @description 一条用户修订的对外回显：内部四档层次 + 意见原文（D4 spec §7）。
         *
         *     历史从**待办列表**重建（决议事件带着当时的载荷），与 ``stage_state.user_revisions``
         *     是两本账：前者是「用户在这张闸门上说过什么」，后者带轮次与落地标记、只给重跑用。
         */
        RevisionOut: {
            /** Layer */
            layer: string;
            /** Text */
            text: string;
        };
        /**
         * SegmentAssetOut
         * @description 本段实际要喂进 ComfyUI 的那几张参考图（D7 §10 / #258）。
         *
         *     ``number`` 是**段内重编号**后的 Picture 号（从 1 连续，段词正文里引用的就是它），
         *     ``original_number`` 是整片台账原号（溯源用）。界面**只读**：段内子集由系统算，
         *     没有任何手改入口——逐段手填等于把闭包算法搬到前端。
         */
        SegmentAssetOut: {
            /** Name */
            name: string;
            /** Number */
            number: number;
            /**
             * Original Number
             * @default 0
             */
            original_number: number;
        };
        /**
         * SegmentOut
         * @description 长片的一个执行段（D6 spec §11）：规划完成后进会话快照，详情一次取到。
         *
         *     ``index`` 是**1 起**的执行段号（与 ``segments/shot-NN.md`` 同号）；段状态在
         *     快照里，不靠 job 历史表达（``JOB_HISTORY_LIMIT`` 对 15 段偏紧）。
         */
        SegmentOut: {
            /** Assets */
            assets?: components["schemas"]["SegmentAssetOut"][];
            /** Declared Frames */
            declared_frames?: number | null;
            /** End Frame */
            end_frame?: number | null;
            /**
             * End Hook
             * @default
             */
            end_hook: string;
            /** End S */
            end_s: number;
            /** Export Frames */
            export_frames?: number | null;
            /** Give Images */
            give_images?: string[];
            /** Guided */
            guided?: boolean | null;
            /** Index */
            index: number;
            /**
             * Prompt Artifact Id
             * @default
             */
            prompt_artifact_id: string;
            /**
             * Rejection Reason
             * @default
             */
            rejection_reason: string;
            /** Segment Type */
            segment_type?: string | null;
            /** Shots In Segment */
            shots_in_segment?: number[];
            /** Start Frame */
            start_frame?: number | null;
            /** Start S */
            start_s: number;
            /**
             * Status
             * @default pending
             */
            status: string;
            /**
             * Summary
             * @default
             */
            summary: string;
            /**
             * Video Asset Id
             * @default
             */
            video_asset_id: string;
        };
        /**
         * SessionCreate
         * @description 建会话的 brief 一次性给全（spec §7；D7 §4.1 放开 ``ref``，D8 放开 ``director``）。
         *
         *     ``mode`` 只作普通字符串：三路线的合法性由 ``_validate_route_parameters`` 判，
         *     这样未知路线能拿到稳定的 ``director_mode_invalid``，而不是 pydantic 那份没有
         *     ``code`` 的通用 422。``ref`` 的参考素材与 ``director`` 的开场首帧**都不进创建
         *     表单**：前者是会话内资产（建好后上传），后者要等阶段 1 才有画幅可比。
         *
         *     导演台三个打包参数（节点 id / 段缓存名 / 段间引导模式）在创建时问齐，与 CLI
         *     「会话开头问一次」是同一时点（``wizard_director.py:119-179``）。``director_mode``
         *     与 ``director_continuity_mode`` 用 ``None`` 表示「没给」：空串与省略在这两个字段
         *     上必须分开——引导模式省略要落默认 ``guide``，而不是落一个空串。
         */
        SessionCreate: {
            /** Canvases */
            canvases?: {
                [key: string]: [
                    number,
                    number
                ];
            };
            /**
             * Clarifications
             * @default
             */
            clarifications: string;
            /** Director Cache Name */
            director_cache_name?: string | null;
            /** Director Continuity Mode */
            director_continuity_mode?: string | null;
            /** Director Mode */
            director_mode?: string | null;
            /** Director Node Id */
            director_node_id?: string | null;
            /**
             * Draft
             * @default
             */
            draft: string;
            /**
             * Duration
             * @default 4
             */
            duration: number;
            /**
             * Language
             * @default Chinese
             */
            language: string;
            /**
             * Mode
             * @default base
             */
            mode: string;
            /** Plot */
            plot: string;
            /**
             * Style
             * @default Cinematic
             */
            style: string;
            /**
             * Variant
             * @default T2VA
             */
            variant: string;
            video_output?: components["schemas"]["VideoOutputIn"] | null;
        };
        /** SessionDetail */
        SessionDetail: {
            /** Action Items */
            action_items?: components["schemas"]["ActionItemOut"][];
            /** Artifacts */
            artifacts?: components["schemas"]["ArtifactInfo"][];
            /** Assets */
            assets?: components["schemas"]["Asset"][];
            brief: components["schemas"]["BriefOut"];
            /** Created At */
            created_at: string;
            current_job?: components["schemas"]["JobSummary"] | null;
            /** Director Notes */
            director_notes?: string[];
            /** Jobs */
            jobs?: components["schemas"]["JobSummary"][];
            /** Last Event Seq */
            last_event_seq: number;
            /** Legacy */
            legacy: boolean;
            /** Mode */
            mode: string;
            /** Plot */
            plot: string;
            /** Revision */
            revision: number;
            /** Segments */
            segments?: components["schemas"]["SegmentOut"][];
            /** Session Id */
            session_id: string;
            /**
             * Status
             * @enum {string}
             */
            status: "created" | "running" | "awaiting_human" | "completed" | "failed" | "cancelled" | "interrupted";
            /** Updated At */
            updated_at: string;
            /** Variant */
            variant: string;
        };
        /** SessionPage */
        SessionPage: {
            /** Items */
            items: components["schemas"]["SessionSummary"][];
            /** Next Cursor */
            next_cursor?: string | null;
        };
        /** SessionSummary */
        SessionSummary: {
            /** Created At */
            created_at: string;
            /** Legacy */
            legacy: boolean;
            /** Mode */
            mode: string;
            /** Plot */
            plot: string;
            /** Revision */
            revision: number;
            /** Session Id */
            session_id: string;
            /**
             * Status
             * @enum {string}
             */
            status: "created" | "running" | "awaiting_human" | "completed" | "failed" | "cancelled" | "interrupted";
            /** Updated At */
            updated_at: string;
            /** Variant */
            variant: string;
        };
        /** VideoOutputIn */
        VideoOutputIn: {
            /** Megapixels */
            megapixels: number;
            /** Multiple */
            multiple: number;
            /** Ratio */
            ratio: string;
        };
        /** VideoOutputOut */
        VideoOutputOut: {
            /** Megapixels */
            megapixels: number;
            /** Multiple */
            multiple: number;
            /** Ratio */
            ratio: string;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    get_artifact_content_api_v1_artifacts__artifact_id__content_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 文本产物全文（UTF-8，1 MiB 上限）或媒体产物的整份字节。 */
            200: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/zip": string;
                    "text/plain": string;
                    "video/mp4": string;
                };
            };
            /** @description 媒体产物的单段字节范围（Range: bytes=…）。 */
            206: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description bytes <start>-<end>/<len> */
                    "Content-Range"?: string;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/zip": string;
                    "video/mp4": string;
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 产物不存在、标识越界，或该名字不在登记范围内（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 文本产物超过 1 MiB（problem+json） */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 媒体产物的 Range 越界、语法非法或多段（problem+json）；带 Content-Range: bytes *\/<len> */
            416: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    head_artifact_content_api_v1_artifacts__artifact_id__content_head: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 与 GET 同头、无体。 */
            200: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 媒体产物的字节范围，与 GET 同头、无体。 */
            206: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description bytes <start>-<end>/<len> */
                    "Content-Range"?: string;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 产物不存在、标识越界，或该名字不在登记范围内（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 文本产物超过 1 MiB（problem+json） */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 媒体产物的 Range 越界、语法非法或多段（problem+json）；带 Content-Range: bytes *\/<len> */
            416: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    get_auth_token_api_v1_auth_token_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AuthTokenResponse"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    get_diagnostics_bundle_api_v1_diagnostics_bundle_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 诊断包 zip：清单、环境信息、脱敏配置快照、日志窗口、各会话状态与事件流、文本产物副本（不含密钥与素材媒体）。 */
            200: {
                headers: {
                    /** @description 恒为 no-store（诊断包含本机路径，不许被缓存） */
                    "Cache-Control"?: string;
                    /** @description attachment；文件名按 RFC 5987 编码（filename*=UTF-8''…） */
                    "Content-Disposition"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/zip": string;
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    get_health_api_v1_health_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HealthResponse"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    get_media_api_v1_media__asset_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                asset_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 整份字节；Content-Type 由素材的实际格式决定。 */
            200: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/zip": string;
                    "audio/mpeg": string;
                    "audio/wav": string;
                    "image/jpeg": string;
                    "image/png": string;
                    "image/webp": string;
                    "video/mp4": string;
                    "video/webm": string;
                };
            };
            /** @description 单段字节范围（Range: bytes=…）。 */
            206: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description bytes <start>-<end>/<len> */
                    "Content-Range"?: string;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/zip": string;
                    "audio/mpeg": string;
                    "audio/wav": string;
                    "image/jpeg": string;
                    "image/png": string;
                    "image/webp": string;
                    "video/mp4": string;
                    "video/webm": string;
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 素材不存在、标识越界，或不是素材命名形态（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Range 越界、语法非法或多段（problem+json）；带 Content-Range: bytes *\/<len> */
            416: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    head_media_api_v1_media__asset_id__head: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                asset_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 与 GET 同头、无体。 */
            200: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 与 GET 同头、无体。 */
            206: {
                headers: {
                    /** @description 恒为 bytes（单段） */
                    "Accept-Ranges"?: string;
                    /** @description inline；显示名按 RFC 5987 编码 */
                    "Content-Disposition"?: string;
                    /** @description 本次响应的字节数（HEAD 时为 GET 会给的字节数） */
                    "Content-Length"?: number;
                    /** @description bytes <start>-<end>/<len> */
                    "Content-Range"?: string;
                    /** @description 缓存标识（素材即内容哈希） */
                    ETag?: string;
                    /** @description 文件修改时间（HTTP-date） */
                    "Last-Modified"?: string;
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 素材不存在、标识越界，或不是素材命名形态（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Range 越界、语法非法或多段（problem+json）；带 Content-Range: bytes *\/<len> */
            416: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    list_sessions_api_v1_sessions_get: {
        parameters: {
            query?: {
                limit?: number;
                cursor?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionPage"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    create_session_api_v1_sessions_post: {
        parameters: {
            query?: never;
            header?: {
                "Idempotency-Key"?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SessionCreate"];
            };
        };
        responses: {
            /** @description 幂等重放：返回第一次的结果 */
            200: {
                headers: {
                    /** @description 重放标记；首次创建不返回该头 */
                    "Idempotency-Replayed"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionDetail"];
                };
            };
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionDetail"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    get_session_api_v1_sessions__session_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionDetail"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    list_action_items_api_v1_sessions__session_id__action_items_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ActionItemOut"][];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    resolve_action_item_api_v1_sessions__session_id__action_items__item_id__resolve_post: {
        parameters: {
            query?: never;
            header?: {
                "Idempotency-Key"?: string | null;
            };
            path: {
                session_id: string;
                item_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ActionItemResolve"];
            };
        };
        responses: {
            /** @description 决议已受理，重跑在后台执行（异步）。帧链通过审查不启动任务（等待帧提交），此时响应体为 null。 */
            202: {
                headers: {
                    /** @description 重放标记；首次决议不返回该头 */
                    "Idempotency-Replayed"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JobSummary"] | null;
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 待办不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话/待办状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    list_artifacts_api_v1_sessions__session_id__artifacts_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ArtifactInfo"][];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    list_assets_api_v1_sessions__session_id__assets_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Asset"][];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    upload_asset_api_v1_sessions__session_id__assets_post: {
        parameters: {
            query?: never;
            header?: {
                "Idempotency-Key"?: string | null;
            };
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "multipart/form-data": {
                    /** Format: binary */
                    file: string;
                    /** @description 参考素材的语义名；带上即登记进台账（会话内唯一，起跑后不允许再登记），不带则按普通素材上传。 */
                    name?: string;
                };
            };
        };
        responses: {
            /** @description 幂等重放：返回第一次写入的素材 */
            200: {
                headers: {
                    /** @description 重放标记；首次上传不返回该头 */
                    "Idempotency-Replayed"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Asset"];
                };
            };
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Asset"];
                };
            };
            /** @description 请求不是可解析的 multipart 表单，或缺 file 字段（problem+json） */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 历史会话只读、幂等键已用于另一份内容，或 ref 会话已起跑（asset_locked）（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 超过该类型上限，已中止写入（problem+json） */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 魔数不在白名单，或声明的扩展名与内容不符（problem+json） */
            415: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 磁盘写入失败（problem+json） */
            507: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    delete_asset_api_v1_sessions__session_id__assets__asset_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
                asset_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 素材不存在，或不属于 URL 里那个会话（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description base 会话调用（route_mismatch）、会话已起跑（asset_locked）或历史会话只读（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    stream_events_api_v1_sessions__session_id__events_get: {
        parameters: {
            query?: {
                last_event_id?: string | null;
            };
            header?: {
                "Last-Event-ID"?: string | null;
            };
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 事件流（SSE）。快照优先：先 GET 详情拿快照，再订阅本流；断线重连带 Last-Event-ID（或 ?last_event_id=）补发序号更大的事件；序号超出事件流尾号时响应头带 X-Resync-Required: true，要求客户端重取快照。每 15 秒一条心跳注释行；同一会话允许多条并发订阅。 */
            200: {
                headers: {
                    /** @description 缺口补不上，需要重新 GET 详情拿快照 */
                    "X-Resync-Required"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "text/event-stream": string;
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 资源不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Last-Event-ID 不是事件序号（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    start_job_api_v1_sessions__session_id__jobs_post: {
        parameters: {
            query?: never;
            header?: {
                "Idempotency-Key"?: string | null;
            };
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 任务已受理（异步执行） */
            202: {
                headers: {
                    /** @description 重放标记；首次起任务不返回该头 */
                    "Idempotency-Replayed"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JobSummary"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 任务不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话/任务状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    get_job_api_v1_sessions__session_id__jobs__job_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
                job_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JobSummary"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 任务不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话/任务状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    cancel_job_api_v1_sessions__session_id__jobs__job_id__cancel_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
                job_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["JobSummary"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 任务不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话/任务状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
    reopen_segment_api_v1_sessions__session_id__segments__index__reopen_post: {
        parameters: {
            query?: never;
            header?: {
                "Idempotency-Key"?: string | null;
            };
            path: {
                session_id: string;
                index: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 段已回到等待交视频，返回新挂出的 segment_video 待办 */
            200: {
                headers: {
                    /** @description 重放标记；首次换段不返回该头 */
                    "Idempotency-Replayed"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ActionItemOut"];
                };
            };
            /** @description 缺少或无效的工作台访问令牌（problem+json） */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description Host/Origin 不是本机回环地址（problem+json） */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话或段不存在（problem+json） */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 会话/段状态冲突（problem+json） */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 请求参数校验失败（problem+json） */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
            /** @description 服务器内部错误（problem+json） */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["ProblemDetail"];
                };
            };
        };
    };
}
